from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


def _load_env(path: str = "/root/.hermes/.env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load_env()

app = FastAPI(title="GagaPhone Hermes Alarm Bridge")


class RenderRequest(BaseModel):
    alarm_id: int | None = None
    run_id: int | None = None
    name: str = ""
    prompt: str
    timezone: str = "Asia/Shanghai"
    scheduled_at: str | None = None


def _check_auth(authorization: str | None) -> None:
    token = os.getenv("GAGAPHONE_RENDER_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="bridge token is not configured")
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health():
    return {"ok": True, "service": "gagaphone-hermes-bridge"}


@app.post("/gagaphone/render-alarm")
def render_alarm(req: RenderRequest, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    full_prompt = (
        "你是 GagaPhone 动态闹钟口播生成器。只输出一段简短中文口播正文，不要 Markdown，不要解释。"
        "口播需适合电话播放，80 字以内。不要执行命令，不要创建闹钟，不要声称已经拨号。\n"
        f"闹钟名称：{req.name}\n"
        f"计划时间：{req.scheduled_at or ''}，时区：{req.timezone}\n"
        f"动态任务：{prompt}\n"
    )
    try:
        proc = subprocess.run(
            ["/root/.local/bin/hermes", "-z", full_prompt, "--safe-mode"],
            cwd="/root/.hermes",
            text=True,
            capture_output=True,
            timeout=float(os.getenv("GAGAPHONE_RENDER_TIMEOUT_SECONDS", "25")),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Hermes dynamic render timed out")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Hermes render failed").strip()[:500]
        raise HTTPException(status_code=502, detail=detail)
    text = proc.stdout.strip()
    if not text:
        raise HTTPException(status_code=502, detail="Hermes returned empty text")
    return {"ok": True, "text": text[:500]}
