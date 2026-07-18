from __future__ import annotations

import json
import os
import uuid
from typing import Any

import requests
from tools.registry import tool_error, tool_result

API_BASE = os.getenv("GAGAPHONE_API_BASE", "http://127.0.0.1:18701").rstrip("/")
API_TOKEN = os.getenv("GAGAPHONE_ALARM_TOKEN", "")
TIMEOUT = float(os.getenv("GAGAPHONE_API_TIMEOUT_SECONDS", "15"))

EXTENSION_ALIASES = {
    "办公室电话": "559",
    "办公室": "559",
    "office": "559",
    "fanvil": "559",
    "家里电话": "1801",
    "家里": "1801",
    "home": "1801",
    "我的手机": "551",
    "手机": "551",
    "软电话": "551",
    "mobile": "551",
}


def _available() -> bool:
    return bool(API_BASE and API_TOKEN)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _normalize_extension(value: str) -> str:
    raw = str(value or "").strip()
    return EXTENSION_ALIASES.get(raw, raw)


def _request(method: str, path: str, *, payload: dict | None = None, headers: dict | None = None) -> dict:
    if not _available():
        raise RuntimeError("GagaPhone API token or base URL is not configured")
    url = f"{API_BASE}{path}"
    resp = requests.request(method, url, json=payload, headers=_headers(headers), timeout=TIMEOUT)
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text[:500]}
    if resp.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else str(data)
        raise RuntimeError(f"GagaPhone API {resp.status_code}: {msg}")
    return data


def _alarm_summary(data: dict) -> dict:
    alarm = data.get("alarm") or data
    return {
        "alarm_id": alarm.get("id") or data.get("alarm_id"),
        "name": alarm.get("name"),
        "status": alarm.get("status") or data.get("status"),
        "next_run_at": alarm.get("next_run_at") or data.get("next_run_at"),
        "timezone": alarm.get("timezone"),
        "target_extension": alarm.get("target_extension"),
        "schedule_type": alarm.get("schedule_type"),
        "content_type": alarm.get("content_type"),
        "summary": (alarm.get("static_text") or alarm.get("dynamic_prompt") or "")[:80],
    }


def _handle_create(args, **kw):
    try:
        target = _normalize_extension(args.get("target_extension") or args.get("target") or "")
        payload = {
            "name": args.get("name") or "微信闹钟",
            "source": "hermes_wechat",
            "source_user": args.get("source_user") or "wechat",
            "target_extension": target,
            "schedule_type": args.get("schedule_type") or "once",
            "run_at": args.get("run_at") or None,
            "cron_expression": args.get("cron_expression") or "",
            "timezone": args.get("timezone") or "Asia/Shanghai",
            "content_type": args.get("content_type") or "static",
            "static_text": args.get("static_text") or "",
            "dynamic_provider": "hermes" if (args.get("content_type") == "dynamic") else "",
            "dynamic_prompt": args.get("dynamic_prompt") or "",
            "call_mode": args.get("call_mode") or "ring_then_play",
            "retry_count": int(args.get("retry_count", 2) or 0),
            "retry_interval_seconds": int(args.get("retry_interval_seconds", 60) or 60),
        }
        idem = args.get("idempotency_key") or kw.get("message_id") or uuid.uuid4().hex
        data = _request("POST", "/api/v1/alarms", payload=payload, headers={"Idempotency-Key": str(idem)})
        return tool_result({"ok": True, "alarm": _alarm_summary(data), "message": "已在 GagaPhone 统一闹钟中心创建，调度由 GagaPhone 执行。"})
    except Exception as exc:
        return tool_error(str(exc))


def _handle_list(args, **kw):
    try:
        data = _request("GET", "/api/v1/alarms")
        alarms = data.get("alarms", [])
        query = str(args.get("query") or "").strip()
        if query:
            alarms = [a for a in alarms if query in str(a.get("name", "")) or query in str(a.get("static_text", "")) or query in str(a.get("dynamic_prompt", ""))]
        return tool_result({"ok": True, "alarms": alarms[:50], "count": len(alarms)})
    except Exception as exc:
        return tool_error(str(exc))


def _handle_get(args, **kw):
    try:
        alarm_id = int(args.get("alarm_id") or args.get("id"))
        return tool_result(_request("GET", f"/api/v1/alarms/{alarm_id}"))
    except Exception as exc:
        return tool_error(str(exc))


def _handle_update(args, **kw):
    try:
        alarm_id = int(args.get("alarm_id") or args.get("id"))
        payload = {k: v for k, v in args.items() if k not in {"alarm_id", "id"} and v is not None}
        if "target_extension" in payload:
            payload["target_extension"] = _normalize_extension(payload["target_extension"])
        data = _request("PATCH", f"/api/v1/alarms/{alarm_id}", payload=payload)
        return tool_result({"ok": True, "alarm": _alarm_summary(data)})
    except Exception as exc:
        return tool_error(str(exc))


def _id_action(args, action: str, method: str = "POST"):
    alarm_id = int(args.get("alarm_id") or args.get("id"))
    return _request(method, f"/api/v1/alarms/{alarm_id}{action}")


def _handle_delete(args, **kw):
    try:
        data = _id_action(args, "", method="DELETE")
        return tool_result(data)
    except Exception as exc:
        return tool_error(str(exc))


def _handle_pause(args, **kw):
    try:
        return tool_result(_id_action(args, "/pause"))
    except Exception as exc:
        return tool_error(str(exc))


def _handle_resume(args, **kw):
    try:
        return tool_result(_id_action(args, "/resume"))
    except Exception as exc:
        return tool_error(str(exc))


def _handle_skip(args, **kw):
    try:
        return tool_result(_id_action(args, "/skip-next"))
    except Exception as exc:
        return tool_error(str(exc))


def _handle_test(args, **kw):
    try:
        return tool_result(_id_action(args, "/test"))
    except Exception as exc:
        return tool_error(str(exc))


def _handle_broadcast(args, **kw):
    try:
        payload = {
            "target_extension": _normalize_extension(args.get("target_extension") or args.get("target") or ""),
            "text": args.get("text") or "",
            "title": args.get("title") or "Hermes 即时广播",
        }
        return tool_result(_request("POST", "/api/v1/alarms/broadcast-now", payload=payload))
    except Exception as exc:
        return tool_error(str(exc))


ALARM_BASE_PROPS = {
    "name": {"type": "string", "description": "闹钟名称。"},
    "target_extension": {"type": "string", "description": "目标分机或别名：办公室电话=559，家里电话=1801，我的手机=551。"},
    "schedule_type": {"type": "string", "enum": ["once", "cron", "interval"], "description": "一次性用 once，重复规则用 cron，间隔秒数用 interval。"},
    "run_at": {"type": "string", "description": "一次性闹钟的带时区 ISO 8601 时间，例如 2026-07-18T21:00:00+08:00。"},
    "cron_expression": {"type": "string", "description": "cron 表达式，如每天 08:00 为 0 8 * * *；interval 时填秒数。"},
    "timezone": {"type": "string", "default": "Asia/Shanghai"},
    "content_type": {"type": "string", "enum": ["static", "dynamic"]},
    "static_text": {"type": "string", "description": "固定播报文字。"},
    "dynamic_prompt": {"type": "string", "description": "动态播报任务，例如 查询深圳天气和今天日程并生成简短中文口播。"},
    "call_mode": {"type": "string", "enum": ["ring_then_play", "intercom"], "default": "ring_then_play"},
    "retry_count": {"type": "integer", "default": 2},
    "retry_interval_seconds": {"type": "integer", "default": 60},
    "idempotency_key": {"type": "string", "description": "微信消息重试幂等 key。"},
}

SCHEMAS = {
    "create_gagaphone_alarm": {"name": "create_gagaphone_alarm", "description": "在 GagaPhone 统一闹钟中心创建电话闹钟。Hermes 不创建 cron；所有调度都由 GagaPhone 执行。创建成功后回复名称、精确时间、时区、分机、是否重复、内容摘要和任务 ID。", "parameters": {"type": "object", "properties": ALARM_BASE_PROPS, "required": ["name", "target_extension", "schedule_type", "content_type"]}},
    "list_gagaphone_alarms": {"name": "list_gagaphone_alarms", "description": "查询 GagaPhone 当前所有未删除闹钟；可用 query 按名称或内容过滤。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}},
    "get_gagaphone_alarm": {"name": "get_gagaphone_alarm", "description": "按 GagaPhone 闹钟 ID 查询详情。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}},
    "update_gagaphone_alarm": {"name": "update_gagaphone_alarm", "description": "修改 GagaPhone 闹钟。若描述匹配多个闹钟，先 list 返回候选，不要猜测。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}, **ALARM_BASE_PROPS}, "required": ["alarm_id"]}},
    "delete_gagaphone_alarm": {"name": "delete_gagaphone_alarm", "description": "删除或取消 GagaPhone 闹钟。周期闹钟删除后整个重复任务停止。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}},
    "pause_gagaphone_alarm": {"name": "pause_gagaphone_alarm", "description": "暂停 GagaPhone 闹钟。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}},
    "resume_gagaphone_alarm": {"name": "resume_gagaphone_alarm", "description": "恢复 GagaPhone 闹钟。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}},
    "skip_next_gagaphone_alarm": {"name": "skip_next_gagaphone_alarm", "description": "跳过 GagaPhone 周期闹钟的下一次触发；一次性闹钟会变为 completed。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}},
    "test_gagaphone_alarm": {"name": "test_gagaphone_alarm", "description": "立即测试 GagaPhone 闹钟，会真实呼叫目标分机。", "parameters": {"type": "object", "properties": {"alarm_id": {"type": "integer"}}, "required": ["alarm_id"]}},
    "broadcast_gagaphone_now": {"name": "broadcast_gagaphone_now", "description": "立即呼叫指定 GagaPhone 分机并播放一段文字；不创建闹钟，不执行 shell。", "parameters": {"type": "object", "properties": {"target_extension": {"type": "string"}, "text": {"type": "string"}, "title": {"type": "string"}}, "required": ["target_extension", "text"]}},
}

HANDLERS = {
    "create_gagaphone_alarm": _handle_create,
    "list_gagaphone_alarms": _handle_list,
    "get_gagaphone_alarm": _handle_get,
    "update_gagaphone_alarm": _handle_update,
    "delete_gagaphone_alarm": _handle_delete,
    "pause_gagaphone_alarm": _handle_pause,
    "resume_gagaphone_alarm": _handle_resume,
    "skip_next_gagaphone_alarm": _handle_skip,
    "test_gagaphone_alarm": _handle_test,
    "broadcast_gagaphone_now": _handle_broadcast,
}


def register(ctx) -> None:
    for name, handler in HANDLERS.items():
        ctx.register_tool(
            name=name,
            toolset="gagaphone",
            schema=SCHEMAS[name],
            handler=handler,
            check_fn=_available,
            emoji="☎️",
        )
