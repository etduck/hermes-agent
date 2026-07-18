"""Deterministic GagaPhone alarm fallback for explicit Weixin static alarms.

This module intentionally exposes only narrow GagaPhone alarm operations. It is
used before model dispatch so clear static alarm requests still work when the
model provider is temporarily unavailable. It never executes shell commands.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

API_BASE = os.getenv("GAGAPHONE_API_BASE", "http://127.0.0.1:18701").rstrip("/")
API_TOKEN = os.getenv("GAGAPHONE_ALARM_TOKEN", "")
TIMEOUT = float(os.getenv("GAGAPHONE_API_TIMEOUT_SECONDS", "15"))
TZ = ZoneInfo("Asia/Shanghai")

EXTENSION_ALIASES = {
    "家里电话": "1801",
    "家里的电话": "1801",
    "家里": "1801",
    "home": "1801",
    "办公室电话": "559",
    "办公室": "559",
    "我的手机": "551",
    "手机": "551",
}


@dataclass
class ParsedAlarm:
    target_extension: str
    run_at: datetime
    static_text: str
    call_mode: str
    name: str = "微信静态闹钟"


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _request(method: str, path: str, payload: dict[str, Any] | None = None, idem: str | None = None) -> dict[str, Any]:
    if not API_TOKEN:
        raise RuntimeError("GagaPhone alarm token is not configured")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers=_headers({"Idempotency-Key": idem} if idem else None),
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:500]
        try:
            body = json.loads(raw)
            msg = body.get("error") or body.get("message") or raw
        except Exception:
            msg = raw
        raise RuntimeError(f"GagaPhone API HTTP {exc.code}: {msg}") from exc


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").replace("：", ":"))


def _chinese_int(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    table = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if value in table:
        return table[value]
    if value.startswith("十") and len(value) == 2 and value[1] in table:
        return 10 + table[value[1]]
    if value.endswith("十") and len(value) == 2 and value[0] in table:
        return table[value[0]] * 10
    if "十" in value:
        left, right = value.split("十", 1)
        if left in table and right in table:
            return table[left] * 10 + table[right]
    return None


def _extension(text: str) -> str | None:
    for key, value in EXTENSION_ALIASES.items():
        if key in text:
            return value
    match = re.search(r"(?<!\d)(551|559|1801)(?!\d)", text)
    return match.group(1) if match else None


def _relative_run_at(text: str, now: datetime) -> datetime | None:
    if "半小时后" in text:
        return now + timedelta(minutes=30)
    match = re.search(r"([0-9]+|[零一二两三四五六七八九十]+)分钟后", text)
    if match:
        minutes = _chinese_int(match.group(1))
        if minutes:
            return now + timedelta(minutes=minutes)
    match = re.search(r"([0-9]+|[零一二两三四五六七八九十]+)小时后", text)
    if match:
        hours = _chinese_int(match.group(1))
        if hours:
            return now + timedelta(hours=hours)
    match = re.search(r"([0-9]+|[零一二两三四五六七八九十]+)秒后", text)
    if match:
        seconds = _chinese_int(match.group(1))
        if seconds:
            return now + timedelta(seconds=seconds)
    return None


def _absolute_run_at(text: str, now: datetime) -> datetime | None:
    match = re.search(r"(?:今天)?([0-2]?\d)(?:[:点])([0-5]?\d)?分?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= now - timedelta(seconds=30):
        run_at += timedelta(days=1)
    return run_at


def parse_static_alarm(text: str, now: datetime | None = None) -> ParsedAlarm | None:
    normalized = _normalize(text)
    if "播报" not in normalized or not any(word in normalized for word in ("让", "拨打", "电话")):
        return None
    target = _extension(normalized)
    if not target:
        return None
    content_match = re.search(r"播报[:：]?(?P<content>.+)$", text or "")
    if not content_match:
        return None
    content = content_match.group("content").strip()
    if not content:
        return None
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    run_at = _relative_run_at(normalized, now) or _absolute_run_at(normalized, now)
    if not run_at:
        return None
    return ParsedAlarm(
        target_extension=target,
        run_at=run_at,
        static_text=content,
        call_mode="intercom" if "自动接听" in normalized else "ring_then_play",
    )


def _format_alarm_response(data: dict[str, Any]) -> str:
    alarm = data.get("alarm") or data
    alarm_id = alarm.get("id") or data.get("alarm_id")
    next_run_at = alarm.get("next_run_at") or alarm.get("run_at")
    target = alarm.get("target_extension")
    text = (alarm.get("static_text") or "")[:80]
    mode = "自动接听" if alarm.get("call_mode") == "intercom" else "响铃后播放"
    return (
        "已设置 GagaPhone 闹钟：\n"
        f"任务 ID：{alarm_id}\n"
        f"时间：{next_run_at} Asia/Shanghai\n"
        f"分机：{target}（{mode}）\n"
        f"播报：{text}\n"
        "调度由 GagaPhone 统一闹钟中心执行。"
    )


def create_static_alarm(text: str, *, message_id: str | None = None, source_user: str = "wechat") -> str | None:
    parsed = parse_static_alarm(text)
    if not parsed:
        return None
    idem = message_id or f"wechat-static:{source_user}:{hash(text)}"
    payload = {
        "name": parsed.name,
        "source": "hermes_wechat",
        "source_user": source_user or "wechat",
        "target_extension": parsed.target_extension,
        "schedule_type": "once",
        "run_at": parsed.run_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "content_type": "static",
        "static_text": parsed.static_text,
        "call_mode": parsed.call_mode,
        "retry_count": 2,
        "retry_interval_seconds": 60,
    }
    data = _request("POST", "/api/v1/alarms", payload, idem=idem)
    return _format_alarm_response(data)


def handle_gagaphone_alarm_fallback(text: str, *, message_id: str | None = None, source_user: str = "wechat") -> str | None:
    normalized = _normalize(text)
    if not any(marker in normalized.lower() for marker in ("gagaphone", "1801", "559", "551", "家里", "办公室")):
        return None
    return create_static_alarm(text, message_id=message_id, source_user=source_user)
