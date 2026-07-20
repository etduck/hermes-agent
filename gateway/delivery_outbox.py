"""Persistent delivery outbox for Weixin transcription result files.

The outbox deliberately tracks local transcription completion separately from
platform-accepted file delivery.  It only stores metadata needed to retry and
dedupe delivery; transcript content and secrets are never written to logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)

STATE_RECEIVED = "received"
STATE_TRANSCRIBING = "transcribing"
STATE_TRANSCRIPTION_COMPLETED = "transcription_completed"
STATE_DELIVERY_QUEUED = "delivery_queued"
STATE_DELIVERY_SENDING = "delivery_sending"
STATE_DELIVERY_SENT = "delivery_sent"
STATE_DELIVERY_FAILED = "delivery_failed"
STATE_COMPLETED = "completed"

TRANSCRIPTION_SUCCESS_NOTICE = "视频已转录，转录结果已经发送成功。"
TRANSCRIPTION_FAILURE_NOTICE = "视频转录已经完成，但转录结果发送失败，系统将继续保留文件以便重试。"

_TRANSCRIPTION_HINT_RE = re.compile(r"(transcri|transcript|转录|stt[-_]work)", re.IGNORECASE)
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _outbox_dir() -> Path:
    return _home() / "delivery_outbox"


def _state_path() -> Path:
    return _outbox_dir() / "outbox.json"


def _masked(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "***" + value[-4:]


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"jobs": {}, "deliveries": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.error("delivery_outbox state load failed path=%s error=%s", path, exc)
        return {"jobs": {}, "deliveries": {}}
    if not isinstance(data, dict):
        return {"jobs": {}, "deliveries": {}}
    data.setdefault("jobs", {})
    data.setdefault("deliveries", {})
    return data


def _save_state(state: dict[str, Any]) -> None:
    directory = _outbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(path: Path, suffix: str | None = None) -> str:
    name = path.name or f"delivery-{uuid.uuid4().hex}"
    name = re.sub(r"[\\/\x00-\x1f]", "_", name)
    if suffix:
        stem = Path(name).stem or "delivery"
        name = f"{stem}{suffix}"
    return name


def _copy_durable(source: Path, job_id: str, *, name: str | None = None) -> Path:
    target_dir = _outbox_dir() / "files" / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (name or _safe_name(source))
    if target.exists():
        if target.stat().st_size == source.stat().st_size:
            return target
        target = target_dir / f"{target.stem}-{uuid.uuid4().hex[:8]}{target.suffix}"
    shutil.copy2(source, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def _stable_job_id(chat_id: str, files: Iterable[str], source_message_id: str | None) -> str:
    digest = hashlib.sha256()
    digest.update(chat_id.encode("utf-8", "surrogatepass"))
    if source_message_id:
        digest.update(source_message_id.encode("utf-8", "surrogatepass"))
    for item in files:
        path = Path(item)
        digest.update(str(path).encode("utf-8", "surrogatepass"))
        try:
            stat = path.stat()
            digest.update(str(stat.st_size).encode())
            digest.update(str(int(stat.st_mtime)).encode())
        except OSError:
            pass
    return "stt-" + digest.hexdigest()[:24]


def _delivery_id(job_id: str, chat_id: str, path: Path, file_hash: str, index: int) -> str:
    digest = hashlib.sha256()
    digest.update(job_id.encode())
    digest.update(chat_id.encode("utf-8", "surrogatepass"))
    digest.update(file_hash.encode())
    digest.update(path.name.encode("utf-8", "surrogatepass"))
    digest.update(str(index).encode())
    return "dlv-" + digest.hexdigest()[:24]


def is_transcription_delivery(files: Iterable[str], text: str | None = None) -> bool:
    if text and _TRANSCRIPTION_HINT_RE.search(text):
        return True
    for item in files:
        if _TRANSCRIPTION_HINT_RE.search(str(item)):
            return True
    return False


def _result_is_platform_accepted(result: Any) -> bool:
    if not result or not getattr(result, "success", False):
        return False
    if getattr(result, "message_id", None) or getattr(result, "media_id", None):
        return True
    raw = getattr(result, "raw_response", None)
    return bool(raw)


def _retryable_from_result(result: Any, exc: Exception | None = None) -> bool:
    if result is not None and getattr(result, "retryable", False):
        return True
    text = ""
    if result is not None:
        text = str(getattr(result, "error", "") or "")
    if exc is not None:
        text = str(exc)
    lowered = text.lower()
    return any(token in lowered for token in ("timeout", "rate", "429", "500", "502", "503", "504", "temporar", "connection", "cdn"))


def _should_use_fallback(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(
        token in lowered
        for token in (
            "too large",
            "file size",
            "oversize",
            "unsupported",
            "not allowed",
            "bad format",
            "invalid media",
            "mime",
            "extension",
            "file type",
        )
    )


def _backoffs() -> list[float]:
    configured = os.environ.get("HERMES_DELIVERY_OUTBOX_BACKOFFS")
    if configured:
        values: list[float] = []
        for item in configured.split(","):
            try:
                values.append(max(0.0, float(item.strip())))
            except ValueError:
                continue
        if values:
            return values
    return [2.0, 5.0, 15.0]


def _max_file_bytes() -> int:
    raw = os.environ.get("HERMES_TRANSCRIPTION_DELIVERY_MAX_FILE_BYTES")
    try:
        value = int(raw) if raw else 20 * 1024 * 1024
    except ValueError:
        value = 20 * 1024 * 1024
    return max(1024, value)


def _text_from_transcript_json(path: Path) -> str | None:
    candidates = []
    if path.suffix.lower() == ".json":
        candidates.append(path)
    candidates.append(path.with_name("transcript.json"))
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        parts: list[str] = []
        if isinstance(data, dict):
            for key in ("text", "transcript"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            segments = data.get("segments") or data.get("chunks") or data.get("items")
            if isinstance(segments, list):
                for item in segments:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                    elif isinstance(item, str) and item.strip():
                        parts.append(item.strip())
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
        if parts:
            return "\n".join(parts)
    return None


def _split_text_to_files(text: str, job_id: str, basename: str) -> list[Path]:
    max_bytes = _max_file_bytes()
    target_dir = _outbox_dir() / "files" / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    encoded_parts: list[bytes] = []
    current = bytearray()
    for line in text.splitlines(keepends=True) or [text]:
        raw = line.encode("utf-8")
        if current and len(current) + len(raw) > max_bytes:
            encoded_parts.append(bytes(current))
            current.clear()
        while len(raw) > max_bytes:
            encoded_parts.append(raw[:max_bytes])
            raw = raw[max_bytes:]
        current.extend(raw)
    if current or not encoded_parts:
        encoded_parts.append(bytes(current))
    total = len(encoded_parts)
    paths: list[Path] = []
    stem = Path(basename).stem or "视频转录"
    for idx, payload in enumerate(encoded_parts, start=1):
        name = f"{stem}_{idx:02d}.txt" if total > 1 else f"{stem}.txt"
        path = target_dir / name
        path.write_bytes(payload)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        paths.append(path)
    return paths


def _fallback_files_for(source: Path, job_id: str) -> list[Path]:
    suffix = source.suffix.lower()
    text: str | None = None
    if suffix in {".txt", ".md", ".json"}:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = None
    if text is None:
        text = _text_from_transcript_json(source)
    if not text:
        return []
    return _split_text_to_files(text, job_id, "视频转录.txt")


def _prepare_initial_files(files: list[str], job_id: str) -> tuple[list[Path], list[str]]:
    prepared: list[Path] = []
    errors: list[str] = []
    for item in files:
        source = Path(item)
        if not source.exists():
            errors.append(f"file not found: {source}")
            continue
        if not source.is_file():
            errors.append(f"not a regular file: {source}")
            continue
        if source.stat().st_size > _max_file_bytes() and source.suffix.lower() in {".txt", ".md", ".json"}:
            text = source.read_text(encoding="utf-8", errors="replace")
            prepared.extend(_split_text_to_files(text, job_id, source.name))
        else:
            prepared.append(_copy_durable(source, job_id))
    return prepared, errors


async def _send_path(adapter: Any, chat_id: str, path: Path, metadata: Any = None) -> SendResult:
    ext = path.suffix.lower()
    if ext in _AUDIO_EXTS:
        return await adapter.send_voice(chat_id=chat_id, audio_path=str(path), metadata=metadata)
    if ext in _VIDEO_EXTS:
        return await adapter.send_video(chat_id=chat_id, video_path=str(path), metadata=metadata)
    return await adapter.send_document(chat_id=chat_id, file_path=str(path), metadata=metadata)


async def _send_one_delivery(
    adapter: Any,
    chat_id: str,
    path: Path,
    *,
    job_id: str,
    index: int,
    metadata: Any = None,
    backoffs: list[float] | None = None,
) -> tuple[bool, str | None]:
    state = _load_state()
    file_hash = _sha256_file(path)
    delivery_id = _delivery_id(job_id, chat_id, path, file_hash, index)
    record = state["deliveries"].setdefault(delivery_id, {})
    if record.get("status") == STATE_DELIVERY_SENT and (record.get("message_id") or record.get("media_id") or record.get("platform_accepted")):
        logger.info(
            "delivery_outbox idempotent_skip job_id=%s delivery_id=%s file=%s size=%s sha256=%s chat=%s message_id=%s",
            job_id, delivery_id, path.name, path.stat().st_size, file_hash[:16], _masked(chat_id), record.get("message_id"),
        )
        return True, None
    record.update({
        "delivery_id": delivery_id,
        "job_id": job_id,
        "platform": "weixin",
        "chat_id": chat_id,
        "file_path": str(path),
        "file_name": path.name,
        "file_hash": file_hash,
        "file_size": path.stat().st_size,
        "status": STATE_DELIVERY_QUEUED,
        "updated_at": _now(),
    })
    record.setdefault("created_at", _now())
    record.setdefault("attempts", 0)
    _save_state(state)

    delays = backoffs if backoffs is not None else _backoffs()
    last_error: str | None = None
    max_attempts = max(1, len(delays))
    while int(record.get("attempts") or 0) < max_attempts:
        state = _load_state()
        record = state["deliveries"].setdefault(delivery_id, record)
        record["status"] = STATE_DELIVERY_SENDING
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["updated_at"] = _now()
        _save_state(state)
        attempt = int(record["attempts"])
        logger.info(
            "delivery_outbox sending job_id=%s delivery_id=%s attempt=%s file=%s size=%s sha256=%s chat=%s",
            job_id, delivery_id, attempt, path.name, path.stat().st_size, file_hash[:16], _masked(chat_id),
        )
        result: Any = None
        exc: Exception | None = None
        try:
            result = await _send_path(adapter, chat_id, path, metadata=metadata)
        except Exception as err:
            exc = err
        accepted = _result_is_platform_accepted(result)
        state = _load_state()
        record = state["deliveries"].setdefault(delivery_id, record)
        if accepted:
            record.update({
                "status": STATE_DELIVERY_SENT,
                "message_id": getattr(result, "message_id", None),
                "media_id": getattr(result, "media_id", None),
                "platform_accepted": True,
                "platform_code": None,
                "platform_message": None,
                "last_error": None,
                "sent_at": _now(),
                "updated_at": _now(),
            })
            _save_state(state)
            logger.info(
                "delivery_outbox sent job_id=%s delivery_id=%s file=%s message_id=%s media_id=%s",
                job_id, delivery_id, path.name, record.get("message_id"), record.get("media_id"),
            )
            return True, None
        if result is None and exc is None:
            last_error = "send returned empty result"
            retryable = False
        elif exc is not None:
            last_error = str(exc)
            retryable = _retryable_from_result(None, exc)
        else:
            last_error = getattr(result, "error", None) or "send did not return platform acceptance"
            retryable = _retryable_from_result(result)
        record.update({
            "status": STATE_DELIVERY_FAILED,
            "last_error": last_error,
            "retryable": retryable,
            "updated_at": _now(),
        })
        _save_state(state)
        logger.warning(
            "delivery_outbox failed job_id=%s delivery_id=%s attempt=%s retryable=%s error=%s",
            job_id, delivery_id, attempt, retryable, last_error,
        )
        if not retryable or attempt >= max_attempts:
            break
        delay = delays[min(attempt - 1, len(delays) - 1)]
        if delay > 0:
            await asyncio.sleep(delay)
    return False, last_error


async def deliver_weixin_transcription_files(
    adapter: Any,
    chat_id: str,
    files: list[str],
    *,
    metadata: Any = None,
    source_message_id: str | None = None,
    job_id: str | None = None,
    backoffs: list[float] | None = None,
) -> SendResult:
    job_id = job_id or _stable_job_id(chat_id, files, source_message_id)
    state = _load_state()
    job = state["jobs"].setdefault(job_id, {})
    if job.get("status") == STATE_COMPLETED and job.get("final_notice_sent"):
        return SendResult(success=True, message_id=job.get("final_notice_message_id"), raw_response={"job_id": job_id, "platform_accepted": True})
    job.update({
        "job_id": job_id,
        "platform": "weixin",
        "chat_id": chat_id,
        "source_message_id": source_message_id,
        "original_files": list(files),
        "status": STATE_TRANSCRIPTION_COMPLETED,
        "updated_at": _now(),
    })
    job.setdefault("created_at", _now())
    _save_state(state)

    prepared, errors = _prepare_initial_files(files, job_id)
    if errors:
        state = _load_state()
        job = state["jobs"].setdefault(job_id, job)
        job.update({"status": STATE_DELIVERY_FAILED, "last_error": "; ".join(errors), "updated_at": _now()})
        _save_state(state)
        await _send_failure_notice_once(adapter, chat_id, job_id, metadata=metadata)
        return SendResult(success=False, error=job["last_error"], raw_response={"job_id": job_id})

    state = _load_state()
    job = state["jobs"].setdefault(job_id, job)
    job.update({"status": STATE_DELIVERY_QUEUED, "prepared_files": [str(p) for p in prepared], "updated_at": _now()})
    _save_state(state)

    failures: list[str] = []
    for index, path in enumerate(prepared, start=1):
        ok, error = await _send_one_delivery(adapter, chat_id, path, job_id=job_id, index=index, metadata=metadata, backoffs=backoffs)
        if ok:
            continue
        fallback_paths = _fallback_files_for(path, job_id) if _should_use_fallback(error) else []
        if fallback_paths:
            logger.info("delivery_outbox fallback_split job_id=%s file=%s parts=%d", job_id, path.name, len(fallback_paths))
            fallback_ok = True
            for part_index, fallback_path in enumerate(fallback_paths, start=1000 + index * 100):
                part_ok, part_error = await _send_one_delivery(
                    adapter,
                    chat_id,
                    fallback_path,
                    job_id=job_id,
                    index=part_index,
                    metadata=metadata,
                    backoffs=backoffs,
                )
                if not part_ok:
                    fallback_ok = False
                    failures.append(part_error or f"failed to send {fallback_path.name}")
                    break
            if fallback_ok:
                continue
        failures.append(error or f"failed to send {path.name}")

    if failures:
        state = _load_state()
        job = state["jobs"].setdefault(job_id, job)
        job.update({"status": STATE_DELIVERY_FAILED, "last_error": "; ".join(failures), "updated_at": _now()})
        _save_state(state)
        await _send_failure_notice_once(adapter, chat_id, job_id, metadata=metadata)
        return SendResult(success=False, error=job["last_error"], raw_response={"job_id": job_id})

    state = _load_state()
    job = state["jobs"].setdefault(job_id, job)
    job.update({"status": STATE_DELIVERY_SENT, "updated_at": _now()})
    _save_state(state)
    if not job.get("final_notice_sent"):
        notice = await adapter.send(chat_id=chat_id, content=TRANSCRIPTION_SUCCESS_NOTICE, metadata=metadata)
        if not _result_is_platform_accepted(notice):
            err = getattr(notice, "error", None) or "final notice was not platform accepted"
            state = _load_state()
            job = state["jobs"].setdefault(job_id, job)
            job.update({"status": STATE_DELIVERY_FAILED, "last_error": err, "updated_at": _now()})
            _save_state(state)
            return SendResult(success=False, error=err, raw_response={"job_id": job_id})
        state = _load_state()
        job = state["jobs"].setdefault(job_id, job)
        job.update({
            "status": STATE_COMPLETED,
            "final_notice_sent": True,
            "final_notice_message_id": getattr(notice, "message_id", None),
            "completed_at": _now(),
            "updated_at": _now(),
        })
        _save_state(state)
    return SendResult(success=True, message_id=job.get("final_notice_message_id"), raw_response={"job_id": job_id, "platform_accepted": True})


async def _send_failure_notice_once(adapter: Any, chat_id: str, job_id: str, *, metadata: Any = None) -> None:
    state = _load_state()
    job = state["jobs"].setdefault(job_id, {})
    if job.get("failure_notice_sent"):
        return
    try:
        result = await adapter.send(chat_id=chat_id, content=TRANSCRIPTION_FAILURE_NOTICE, metadata=metadata)
    except Exception as exc:
        logger.warning("delivery_outbox failure_notice_failed job_id=%s error=%s", job_id, exc)
        return
    state = _load_state()
    job = state["jobs"].setdefault(job_id, job)
    if _result_is_platform_accepted(result):
        job["failure_notice_sent"] = True
        job["failure_notice_message_id"] = getattr(result, "message_id", None)
        job["updated_at"] = _now()
        _save_state(state)


async def recover_weixin_transcription_outbox(adapter: Any, *, backoffs: list[float] | None = None) -> None:
    state = _load_state()
    jobs = list(state.get("jobs", {}).items())
    for job_id, job in jobs:
        if job.get("platform") != "weixin":
            continue
        if job.get("status") == STATE_COMPLETED and job.get("final_notice_sent"):
            continue
        files = job.get("original_files") or job.get("prepared_files") or []
        chat_id = job.get("chat_id")
        if not chat_id or not files:
            continue
        logger.info("delivery_outbox recovering job_id=%s chat=%s status=%s", job_id, _masked(str(chat_id)), job.get("status"))
        await deliver_weixin_transcription_files(
            adapter,
            str(chat_id),
            [str(p) for p in files],
            job_id=str(job_id),
            backoffs=backoffs,
        )
