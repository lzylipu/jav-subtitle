"""HTTP control plane.

两套鉴权并存：
- 浏览器：HTTP Basic（WEB_USER / WEB_PASSWORD），页面弹出的登录框
- AI / 脚本：`Authorization: Bearer <api_token>`，token 首次启动随机生成，只存在数据库
"""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import pipeline
from .config import Settings, settings_from_env
from .db import (
    add_event, connect, init_db, job_events, load_settings, save_settings,
)
from .ratelimit import limiter
from .scanner import library_tree, scan_media

scheduler = BackgroundScheduler()
security = HTTPBasic(auto_error=False)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def authorized(request: Request,
               credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Basic 或 Bearer 任一通过即可。只读 token 不能改状态。"""
    settings = load_settings()
    token = _bearer(request)
    if token and settings.api_token and secrets.compare_digest(token, settings.api_token):
        if settings.api_token_readonly and request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(403, "该 API token 是只读的，不允许执行写操作")
        return

    password = os.getenv("WEB_PASSWORD", "")
    if not password:
        return
    if (credentials is None
            or not secrets.compare_digest(credentials.password, password)
            or not secrets.compare_digest(credentials.username, os.getenv("WEB_USER", "admin"))):
        raise HTTPException(401, "未授权", headers={"WWW-Authenticate": "Basic"})


def reschedule() -> None:
    settings = load_settings()
    if scheduler.get_job("scan"):
        scheduler.remove_job("scan")
    if settings.schedule_enabled:
        scheduler.add_job(scan_and_queue, "cron", hour=settings.schedule_hour,
                          minute=settings.schedule_minute, id="scan",
                          max_instances=1, coalesce=True)


def scan_and_queue(followup: bool = False) -> dict:
    """扫描=只建索引。是否顺便入队由 auto_enqueue 决定，默认关闭。

    为什么默认不入队：用户要的是「索引 → 看目录里有多少片 → 自己选目录 → 才开跑」。
    偷偷把全库排队等于替用户做了决定，几千部片子可能一夜之间全烧掉翻译额度。
    """
    settings = load_settings()
    result = scan_media(settings.media_root)
    result["queued"] = pipeline.queue_indexed() if settings.auto_enqueue else 0
    result["auto_enqueue"] = settings.auto_enqueue
    if settings.auto_enqueue and not followup:
        scheduler.add_job(scan_and_queue, "date", run_date=datetime.now() + timedelta(seconds=125),
                          kwargs={"followup": True}, id="followup_scan", replace_existing=True)
    return result


def ensure_api_token() -> str:
    settings = load_settings()
    if settings.api_token:
        return settings.api_token
    settings.api_token = "hs_" + secrets.token_urlsafe(32)
    save_settings(settings)
    add_event(0, "已生成 AI 接入 token", "info")
    return settings.api_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("WEB_PASSWORD"):
        raise RuntimeError("必须配置 WEB_PASSWORD 才能启动服务")
    os.umask(0o077)
    init_db()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM settings WHERE key='main'").fetchone():
            save_settings(settings_from_env())
    ensure_api_token()
    pipeline.invalidate_settings_cache()
    pipeline.stop_event.clear()
    pipeline.recover_stale()
    pipeline.start_pools()
    scheduler.start()
    reschedule()
    yield
    scheduler.shutdown(wait=False)
    pipeline.stop_pools()


app = FastAPI(title="NAS 日中字幕流水线", lifespan=lifespan,
              dependencies=[Depends(authorized)])


class SettingsPatch(BaseModel):
    media_root: str | None = None
    whisper_model: str | None = None
    compute_type: str | None = None
    cpu_threads: int | None = None
    beam_size: int | None = None
    asr_batch_size: int | None = None
    extract_workers: int | None = None
    asr_workers: int | None = None
    translate_workers: int | None = None
    translate_rpm: int | None = None
    translate_min_interval: float | None = None
    translate_429_backoff: int | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    translation_batch_size: int | None = None
    translate_retries: int | None = None
    daily_char_budget: int | None = None
    tm_enabled: bool | None = None
    schedule_enabled: bool | None = None
    schedule_hour: int | None = None
    schedule_minute: int | None = None
    keep_japanese_srt: bool | None = None
    keep_audio: bool | None = None
    overwrite_existing: bool | None = None
    min_file_age: int | None = None
    max_attempts: int | None = None
    auto_enqueue: bool | None = None
    api_token: str | None = None
    api_token_readonly: bool | None = None
    web_public_url: str | None = None


class PauseBody(BaseModel):
    reason: str = "手动暂停"


class PriorityBody(BaseModel):
    delta: int = 10


class EnqueueBody(BaseModel):
    path: str = ""              # 空 = 媒体根目录
    force: bool = True          # 用户手动点开始，跳过「文件稳定 120 秒」闸门
    limit: int = 0              # >0 时只入队前 N 部，便于小批量试跑


class TokenBody(BaseModel):
    readonly: bool | None = None


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# ------------------------------------------------------------------ settings


@app.get("/api/settings")
def get_settings():
    return load_settings().safe_dict()


@app.put("/api/settings")
def set_settings(patch: SettingsPatch):
    current = load_settings()
    data = current.safe_dict()
    for key, value in patch.model_dump(exclude_none=True).items():
        if key == "openai_api_key" and value == "********":
            continue
        data[key] = value
    updated = Settings.from_dict(data)
    save_settings(updated)
    pipeline.invalidate_settings_cache()
    reschedule()
    return updated.safe_dict()


# ------------------------------------------------------------------ scanning


@app.post("/api/scan")
def scan():
    try:
        return scan_and_queue(followup=False)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/library")
def library(path: str = ""):
    """按目录浏览已索引的视频数量。path 为空表示媒体根目录。"""
    settings = load_settings()
    try:
        return library_tree(settings.media_root, path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/enqueue")
def enqueue(body: EnqueueBody):
    """把某个目录（含子目录）下已索引的视频加入队列。"""
    settings = load_settings()
    media_root = Path(settings.media_root).expanduser().resolve()
    scope = Path(body.path).expanduser().resolve() if body.path else media_root
    if scope != media_root and not scope.is_relative_to(media_root):
        raise HTTPException(400, "目录不在媒体根目录内")
    if not scope.is_dir():
        raise HTTPException(400, f"目录不存在: {scope}")
    try:
        queued = pipeline.queue_indexed(str(scope), force=body.force, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"queued": queued, "path": str(scope), "counts": pipeline.count_pending(str(scope))}


# ------------------------------------------------------------------ jobs


@app.get("/api/stats")
def get_stats():
    return pipeline.stats()


@app.get("/api/jobs")
def jobs(status: str = "", stage: str = "", q: str = "", page: int = 1, size: int = 50):
    size = max(1, min(size, 200))
    page = max(1, page)
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if stage:
        where.append("stage=?")
        params.append(stage)
    if q:
        where.append("video_path LIKE ?")
        params.append(f"%{q}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM jobs{clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM jobs{clause} ORDER BY priority DESC, id DESC LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        ).fetchall()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.get("/api/jobs/{job_id}/events")
def events(job_id: int):
    return job_events(job_id)


@app.post("/api/jobs/{job_id}/retry")
def retry_one(job_id: int):
    with connect() as conn:
        row = conn.execute("SELECT stage,status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "任务不存在")
        if row["status"] == "running":
            raise HTTPException(409, "任务正在运行")
        conn.execute(
            "UPDATE jobs SET status='pending',attempts=0,next_attempt_at=0,error='',"
            "message='手动重试' WHERE id=?", (job_id,))
    add_event(job_id, "手动重试", "warn")
    return {"ok": True}


@app.post("/api/jobs/retry-failed")
def retry_failed():
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='pending',attempts=0,next_attempt_at=0,error='',"
            "message='批量重试' WHERE status='failed'")
        count = cur.rowcount
    return {"ok": True, "count": count}


@app.post("/api/jobs/{job_id}/priority")
def bump(job_id: int, body: PriorityBody):
    with connect() as conn:
        conn.execute("UPDATE jobs SET priority=priority+? WHERE id=?", (body.delta, job_id))
    return {"ok": True}


# ------------------------------------------------------------------ queue


@app.post("/api/queue/pause")
def pause(body: PauseBody | None = None):
    pipeline.pause((body.reason if body else "") or "手动暂停")
    return {"paused": True, "reason": pipeline.pause_info["reason"]}


@app.post("/api/queue/resume")
def resume():
    pipeline.resume()
    return {"paused": False}


@app.get("/api/queue")
def queue_status():
    return {"paused": pipeline.pause_event.is_set(),
            "reason": pipeline.pause_info["reason"],
            "blocked": pipeline.blocked(),
            "translate_api": limiter.snapshot()}


@app.post("/api/queue/reset-limiter")
def reset_limiter():
    limiter.reset()
    return {"ok": True}


# ------------------------------------------------------------------ AI access


AI_HELP = {
    "name": "NAS 日文字幕流水线",
    "auth": "请求头 Authorization: Bearer <token>，token 在 Web UI 的「AI 接入」页可见",
    "endpoints": [
        {"method": "GET", "path": "/api/ai/overview", "desc": "一屏拿到状态、队列、限流、最近日志"},
        {"method": "GET", "path": "/api/stats", "desc": "计数、转写速度、剩余估算"},
        {"method": "GET", "path": "/api/jobs?status=&stage=&q=&page=&size=", "desc": "任务分页列表"},
        {"method": "GET", "path": "/api/jobs/{id}/events", "desc": "单个任务日志"},
        {"method": "POST", "path": "/api/jobs/{id}/retry", "desc": "重试单个任务"},
        {"method": "POST", "path": "/api/jobs/retry-failed", "desc": "重试全部失败"},
        {"method": "POST", "path": "/api/jobs/{id}/priority", "body": {"delta": 10}, "desc": "调整优先级"},
        {"method": "GET", "path": "/api/library?path=", "desc": "按目录看有多少视频/已完成/未做"},
        {"method": "POST", "path": "/api/scan", "desc": "重新索引媒体目录"},
        {"method": "POST", "path": "/api/enqueue", "body": {"path": "/media/某目录", "limit": 0},
         "desc": "把选中目录加入队列，limit>0 可只跑前 N 部"},
        {"method": "POST", "path": "/api/queue/pause", "body": {"reason": "..."}, "desc": "暂停队列"},
        {"method": "POST", "path": "/api/queue/resume", "desc": "恢复队列"},
        {"method": "GET", "path": "/api/logs?limit=100&since_id=0", "desc": "全局日志"},
        {"method": "GET", "path": "/api/settings", "desc": "读取全部设置（密钥已掩码）"},
        {"method": "PUT", "path": "/api/settings", "desc": "修改设置"},
    ],
}


@app.get("/api/ai/help")
def ai_help():
    return AI_HELP


@app.get("/api/ai/overview")
def ai_overview():
    """给 AI 用的单次快照，省得它连打四五个接口。"""
    state = pipeline.stats()
    with connect() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT id,created_at,level,message,job_id FROM events ORDER BY id DESC LIMIT 30")]
        failed = [dict(r) for r in conn.execute(
            "SELECT id,video_path,stage,error,attempts FROM jobs WHERE status='failed' "
            "ORDER BY id DESC LIMIT 20")]
        running = [dict(r) for r in conn.execute(
            "SELECT id,video_path,stage,progress,message FROM jobs WHERE status='running' "
            "ORDER BY id LIMIT 20")]
    settings = load_settings()
    return {
        "web_url": settings.web_public_url or "",
        "state": state,
        "running": running,
        "failed": failed,
        "recent_events": recent,
        "hint": "POST /api/queue/pause 可暂停；GET /api/ai/help 有完整接口清单",
    }


@app.get("/api/logs")
def logs(limit: int = 100, since_id: int = 0, level: str = ""):
    limit = max(1, min(limit, 500))
    where, params = [], []
    if since_id:
        where.append("id>?")
        params.append(since_id)
    if level:
        where.append("level=?")
        params.append(level)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM events{clause} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/ai/token")
def get_token():
    settings = load_settings()
    return {"token": settings.api_token, "readonly": settings.api_token_readonly,
            "web_url": settings.web_public_url}


@app.post("/api/ai/token/rotate")
def rotate_token(body: TokenBody | None = None):
    settings = load_settings()
    settings.api_token = "hs_" + secrets.token_urlsafe(32)
    if body is not None and body.readonly is not None:
        settings.api_token_readonly = body.readonly
    save_settings(settings)
    add_event(0, "AI 接入 token 已重置", "warn")
    return {"token": settings.api_token, "readonly": settings.api_token_readonly}


@app.put("/api/ai/token")
def set_token_mode(body: TokenBody):
    settings = load_settings()
    if body.readonly is not None:
        settings.api_token_readonly = body.readonly
    save_settings(settings)
    return {"readonly": settings.api_token_readonly}


@app.get("/api/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"数据库不可用: {exc}") from exc
    return {"ok": True, "version": "3.0"}
