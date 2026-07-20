import pytest

from gateway.delivery_outbox import (
    STATE_COMPLETED,
    STATE_DELIVERY_FAILED,
    STATE_DELIVERY_SENT,
    TRANSCRIPTION_FAILURE_NOTICE,
    TRANSCRIPTION_SUCCESS_NOTICE,
    _load_state,
    deliver_weixin_transcription_files,
    recover_weixin_transcription_outbox,
)
from gateway.platforms.base import SendResult


class FakeWeixinAdapter:
    def __init__(self, doc_results=None, text_results=None):
        self.doc_results = list(doc_results or [])
        self.text_results = list(text_results or [])
        self.sent_docs = []
        self.sent_texts = []
        self.sequence = []

    def _next(self, queue, default):
        if queue:
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return default

    async def send_document(self, chat_id, file_path, metadata=None, **kwargs):
        self.sent_docs.append(file_path)
        self.sequence.append(("document", file_path))
        return self._next(self.doc_results, SendResult(success=True, message_id=f"doc-{len(self.sent_docs)}"))

    async def send_video(self, chat_id, video_path, metadata=None, **kwargs):
        return await self.send_document(chat_id, video_path, metadata=metadata)

    async def send_voice(self, chat_id, audio_path, metadata=None, **kwargs):
        return await self.send_document(chat_id, audio_path, metadata=metadata)

    async def send(self, chat_id, content, metadata=None, **kwargs):
        self.sent_texts.append(content)
        self.sequence.append(("text", content))
        return self._next(self.text_results, SendResult(success=True, message_id=f"txt-{len(self.sent_texts)}"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_DELIVERY_OUTBOX_BACKOFFS", "0,0,0")
    yield


def write_file(tmp_path, name="transcript.txt", content="hello transcript"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_transcription_completed_but_file_send_failed_does_not_send_success_notice(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter([SendResult(success=False, error="HTTP 500", retryable=False)])

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-fail", backoffs=[0])

    assert not result.success
    assert TRANSCRIPTION_SUCCESS_NOTICE not in adapter.sent_texts
    assert adapter.sent_texts == [TRANSCRIPTION_FAILURE_NOTICE]


@pytest.mark.asyncio
async def test_first_send_fails_then_second_succeeds_and_final_notice_once(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter([
        SendResult(success=False, error="HTTP 500", retryable=True),
        SendResult(success=True, message_id="doc-ok"),
    ])

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-retry", backoffs=[0, 0])

    assert result.success
    assert len(adapter.sent_docs) == 2
    assert adapter.sent_texts == [TRANSCRIPTION_SUCCESS_NOTICE]


@pytest.mark.asyncio
async def test_upload_success_but_message_send_failure_is_not_delivery_sent(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter([SendResult(success=False, error="sendmessage failed", retryable=False)])

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-send-fail", backoffs=[0])

    assert not result.success
    deliveries = _load_state()["deliveries"].values()
    assert any(record["status"] == STATE_DELIVERY_FAILED for record in deliveries)
    assert TRANSCRIPTION_SUCCESS_NOTICE not in adapter.sent_texts


@pytest.mark.asyncio
async def test_empty_send_return_is_not_success(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter([None])

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-empty", backoffs=[0])

    assert not result.success
    assert "empty result" in result.error
    assert TRANSCRIPTION_SUCCESS_NOTICE not in adapter.sent_texts


@pytest.mark.asyncio
async def test_missing_file_enters_failed_state_without_success_notice(tmp_path):
    missing = tmp_path / "missing-transcript.txt"
    adapter = FakeWeixinAdapter()

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(missing)], job_id="job-missing", backoffs=[0])

    assert not result.success
    assert "file not found" in result.error
    assert not adapter.sent_docs
    assert TRANSCRIPTION_SUCCESS_NOTICE not in adapter.sent_texts


@pytest.mark.asyncio
async def test_large_text_file_is_split_and_success_notice_waits_for_all_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TRANSCRIPTION_DELIVERY_MAX_FILE_BYTES", "1024")
    file_path = write_file(tmp_path, content="a" * 2500)
    adapter = FakeWeixinAdapter()

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-split", backoffs=[0])

    assert result.success
    assert len(adapter.sent_docs) == 3
    assert adapter.sequence[-1] == ("text", TRANSCRIPTION_SUCCESS_NOTICE)


@pytest.mark.asyncio
async def test_any_failed_part_prevents_final_success_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TRANSCRIPTION_DELIVERY_MAX_FILE_BYTES", "1024")
    file_path = write_file(tmp_path, content="a" * 2500)
    adapter = FakeWeixinAdapter([
        SendResult(success=True, message_id="part-1"),
        SendResult(success=False, error="sendmessage failed", retryable=False),
    ])

    result = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-part-fail", backoffs=[0])

    assert not result.success
    assert TRANSCRIPTION_SUCCESS_NOTICE not in adapter.sent_texts
    assert adapter.sent_texts == [TRANSCRIPTION_FAILURE_NOTICE]


@pytest.mark.asyncio
async def test_recover_after_restart_retries_unconfirmed_job(tmp_path):
    file_path = write_file(tmp_path)
    first = FakeWeixinAdapter([
        SendResult(success=False, error="HTTP 500", retryable=True),
        SendResult(success=False, error="HTTP 500", retryable=True),
    ])
    result = await deliver_weixin_transcription_files(first, "user-1", [str(file_path)], job_id="job-recover", backoffs=[0, 0])
    assert not result.success

    second = FakeWeixinAdapter([SendResult(success=True, message_id="doc-after-restart")])
    await recover_weixin_transcription_outbox(second, backoffs=[0, 0, 0])

    assert second.sent_docs
    assert second.sent_texts == [TRANSCRIPTION_SUCCESS_NOTICE]
    assert _load_state()["jobs"]["job-recover"]["status"] == STATE_COMPLETED


@pytest.mark.asyncio
async def test_retry_does_not_resend_already_confirmed_file(tmp_path):
    file_path = write_file(tmp_path)
    first = FakeWeixinAdapter([SendResult(success=True, message_id="doc-1")])
    result = await deliver_weixin_transcription_files(first, "user-1", [str(file_path)], job_id="job-idempotent", backoffs=[0])
    assert result.success

    second = FakeWeixinAdapter()
    result = await deliver_weixin_transcription_files(second, "user-1", [str(file_path)], job_id="job-idempotent", backoffs=[0])

    assert result.success
    assert second.sent_docs == []
    assert second.sent_texts == []


@pytest.mark.asyncio
async def test_duplicate_callback_is_idempotent(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter()

    first = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-duplicate", backoffs=[0])
    second = await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-duplicate", backoffs=[0])

    assert first.success and second.success
    assert len(adapter.sent_docs) == 1
    assert adapter.sent_texts == [TRANSCRIPTION_SUCCESS_NOTICE]


@pytest.mark.asyncio
async def test_no_progress_or_fake_sent_notifications_are_emitted(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter()

    await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-quiet", backoffs=[0])

    assert adapter.sent_texts == [TRANSCRIPTION_SUCCESS_NOTICE]
    assert all("正在发送" not in text and "已完成" not in text for text in adapter.sent_texts)


@pytest.mark.asyncio
async def test_final_notice_is_after_file_delivery(tmp_path):
    file_path = write_file(tmp_path)
    adapter = FakeWeixinAdapter()

    await deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-order", backoffs=[0])

    assert adapter.sequence[0][0] == "document"
    assert adapter.sequence[-1] == ("text", TRANSCRIPTION_SUCCESS_NOTICE)
    state = _load_state()
    assert state["jobs"]["job-order"]["status"] == STATE_COMPLETED
    assert any(record["status"] == STATE_DELIVERY_SENT for record in state["deliveries"].values())


def test_backticked_media_directive_is_extracted(tmp_path):
    from gateway.platforms.base import BasePlatformAdapter

    media_file = tmp_path / "report.pdf"
    media_file.write_bytes(b"%PDF-1.4\n")
    response = f"`MEDIA:{media_file}`"

    media, cleaned = BasePlatformAdapter.extract_media(response)

    assert media == [(str(media_file), False)]
    assert cleaned == ""


@pytest.mark.asyncio
async def test_pending_outbox_retry_does_not_block_plain_text_send(tmp_path):
    file_path = write_file(tmp_path)
    started = __import__("asyncio").Event()
    release = __import__("asyncio").Event()

    class SlowFileAdapter(FakeWeixinAdapter):
        async def send_document(self, chat_id, file_path, metadata=None, **kwargs):
            self.sent_docs.append(file_path)
            self.sequence.append(("document", file_path))
            started.set()
            await release.wait()
            return SendResult(success=False, error="HTTP 500", retryable=True)

    adapter = SlowFileAdapter()
    task = __import__("asyncio").create_task(
        deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-slow", backoffs=[0])
    )
    await __import__("asyncio").wait_for(started.wait(), timeout=1)

    text_result = await adapter.send("user-1", "plain text still works")
    release.set()
    await task

    assert text_result.success
    assert "plain text still works" in adapter.sent_texts
    assert TRANSCRIPTION_SUCCESS_NOTICE not in adapter.sent_texts


@pytest.mark.asyncio
async def test_cooldown_like_file_wait_does_not_block_plain_text_send(tmp_path):
    file_path = write_file(tmp_path)
    started = __import__("asyncio").Event()
    release = __import__("asyncio").Event()

    class CooldownFileAdapter(FakeWeixinAdapter):
        async def send_document(self, chat_id, file_path, metadata=None, **kwargs):
            self.sent_docs.append(file_path)
            self.sequence.append(("document", file_path))
            started.set()
            await release.wait()
            return SendResult(success=False, error="rate limited; cooldown active for 30.0s", retryable=False)

    adapter = CooldownFileAdapter()
    task = __import__("asyncio").create_task(
        deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-cooldown", backoffs=[0])
    )
    await __import__("asyncio").wait_for(started.wait(), timeout=1)

    replies = await __import__("asyncio").gather(
        adapter.send("user-1", "msg-1"),
        adapter.send("user-1", "msg-2"),
        adapter.send("user-1", "msg-3"),
    )
    release.set()
    await task

    assert all(result.success for result in replies)
    assert adapter.sent_texts[:3] == ["msg-1", "msg-2", "msg-3"]


@pytest.mark.asyncio
async def test_final_notice_after_file_send_does_not_deadlock_with_send_lock(tmp_path):
    file_path = write_file(tmp_path)

    class LockedAdapter(FakeWeixinAdapter):
        def __init__(self):
            super().__init__()
            self.lock = __import__("asyncio").Lock()

        async def send_document(self, chat_id, file_path, metadata=None, **kwargs):
            async with self.lock:
                return await super().send_document(chat_id, file_path, metadata=metadata, **kwargs)

        async def send(self, chat_id, content, metadata=None, **kwargs):
            async with self.lock:
                return await super().send(chat_id, content, metadata=metadata, **kwargs)

    adapter = LockedAdapter()
    result = await __import__("asyncio").wait_for(
        deliver_weixin_transcription_files(adapter, "user-1", [str(file_path)], job_id="job-lock", backoffs=[0]),
        timeout=1,
    )

    assert result.success
    assert adapter.sequence[0][0] == "document"
    assert adapter.sequence[-1] == ("text", TRANSCRIPTION_SUCCESS_NOTICE)
