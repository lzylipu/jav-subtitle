"""HTTP control plane.

三套鉴权并存，各走各的路：
- 浏览器：`/login` 表单登录，成功后种 HttpOnly Cookie（不再弹浏览器原生对话框）
- AI / 脚本：`Authorization: Bearer <api_token>`，token 首次启动随机生成，只存数据库
- 老脚本：HTTP Basic 仍然认，但服务端**不再主动索要**，所以浏览器不会弹窗
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import asr, cleanup, pipeline, scraping
from .config import DATA_DIR, Settings, settings_from_env
from .db import (
    add_event, connect, init_db, job_events, load_settings, save_settings,
)
from .ratelimit import limiter
from .translation import (PROVIDER_LABELS, PROVIDERS, build_translator,
                          clean_secret, normalize_provider, parse_fallback)
from .scanner import library_tree, scan_media

scheduler = BackgroundScheduler()
security = HTTPBasic(auto_error=False)

SESSION_COOKIE = "ns_session"
SESSION_TTL = 30 * 86400          # 登录一次管一个月，家用 NAS 没人想天天登
STATIC_DIR = Path(__file__).parent / "static"
# 不需要登录就能访问的路径：登录页本身、登录/退出接口、图标（登录页也要显示 logo）
ANON_PATHS = ("/login", "/api/login", "/api/logout",
              "/static/logo.png", "/static/favicon.png", "/static/apple-touch-icon.png")


class NeedLogin(HTTPException):
    """没登录（401）。注册了专门的 handler：页面跳登录页，接口回 JSON。"""

    def __init__(self) -> None:
        super().__init__(401, "未登录")



def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


PASSWORD_FILE = DATA_DIR / "admin-password.txt"          # 老版本只有一个口令，留着做迁移
ACCOUNT_FILE = DATA_DIR / "account.json"                 # 现在用户名 + 口令一起存这儿
_web_auth: tuple[str, str] | None = None
_web_auth_source: str = ""

MIN_PASSWORD_LEN = 8
MIN_USERNAME_LEN = 2


def _read_password_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_account_file(path: Path) -> tuple[str, str] | None:
    """读 account.json。文件坏了/字段缺了都当作没有，退回自动生成，不挡住启动。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    user = str(raw.get("username") or "").strip()
    password = str(raw.get("password") or "")
    if not user or not password:
        return None
    return user, password


def _write_private_file(path: Path, text: str) -> None:
    """0600 + 原子替换，和字幕写入同一套写法。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_account_file(path: Path, user: str, password: str) -> None:
    payload = json.dumps({"username": user, "password": password},
                         ensure_ascii=False, indent=2)
    _write_private_file(path, payload + "\n")


def web_credentials() -> tuple[str, str]:
    """登录账号口令。环境变量 > account.json > 老口令文件（迁移）> 自动生成。

    设计上「第一次运行自动创建，其余全进 Web UI 配」：
    缺口令不能当成致命错误拒绝启动，否则用户只想跑起来看看都做不到。
    """
    global _web_auth, _web_auth_source
    if _web_auth is not None:
        return _web_auth

    env_user = (os.getenv("WEB_USER") or "").strip()
    env_password = (os.getenv("WEB_PASSWORD") or "").strip()
    if env_password:
        _web_auth = (env_user or "admin", env_password)
        _web_auth_source = "env"
        return _web_auth

    stored = _read_account_file(ACCOUNT_FILE)
    if stored:
        _web_auth, _web_auth_source = stored, "file"
        return _web_auth

    # 老版本只有 admin-password.txt：读出来迁到 account.json，用户不用重新设
    legacy = _read_password_file(PASSWORD_FILE)
    if legacy:
        user = env_user or "admin"
        try:
            _write_account_file(ACCOUNT_FILE, user, legacy)
        except OSError:
            pass
        _web_auth, _web_auth_source = (user, legacy), "legacy"
        return _web_auth

    user = env_user or "admin"
    password = secrets.token_urlsafe(12)
    try:
        _write_account_file(ACCOUNT_FILE, user, password)
        source = "new"
    except OSError:
        # 数据目录不可写也别挡住启动，至少本次运行能登进去。
        source = "memory"
    _web_auth, _web_auth_source = (user, password), source
    return _web_auth


def account_locked_reason() -> str:
    """什么时候 Web UI 里改不了账号口令：只有环境变量强指定时。"""
    if (os.getenv("WEB_PASSWORD") or "").strip():
        return "口令由环境变量 WEB_PASSWORD 指定，网页里改不动。要改请删掉该变量后重启容器。"
    return ""


def update_credentials(username: str, password: str) -> None:
    """改账号口令。写完立刻生效：旧 cookie 因为签名密钥变了会全部失效。"""
    global _web_auth, _web_auth_source
    locked = account_locked_reason()
    if locked:
        raise ValueError(locked)
    user = (username or "").strip()
    if len(user) < MIN_USERNAME_LEN:
        raise ValueError(f"用户名至少 {MIN_USERNAME_LEN} 个字符")
    if any(c.isspace() for c in user):
        raise ValueError("用户名里不能有空格")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"口令至少 {MIN_PASSWORD_LEN} 个字符")
    try:
        _write_account_file(ACCOUNT_FILE, user, password)
    except OSError as exc:
        raise ValueError(f"写不进数据目录（{ACCOUNT_FILE}）：{exc}") from exc
    _web_auth, _web_auth_source = (user, password), "file"


def web_password_source() -> str:
    return _web_auth_source


def reset_web_credentials() -> None:
    """测试与「删掉口令文件后重载」用。"""
    global _web_auth, _web_auth_source
    _web_auth, _web_auth_source = None, ""


# ------------------------------------------------------------------ session
# 不用服务端会话表：cookie 里带「过期时间 + HMAC 签名」，签名密钥由当前口令派生。
# 好处是重启容器不掉登录，改口令则所有旧 cookie 立即失效。


def _session_secret() -> bytes:
    _, password = web_credentials()
    return ("ns-session:" + password).encode("utf-8")


def make_session() -> str:
    expires = int(time.time()) + SESSION_TTL
    sig = hmac.new(_session_secret(), str(expires).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def verify_session(value: str) -> bool:
    if not value or "." not in value:
        return False
    expires_text, sig = value.split(".", 1)
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    if expires < time.time():
        return False
    want = hmac.new(_session_secret(), expires_text.encode("ascii"), hashlib.sha256).hexdigest()
    return secrets.compare_digest(sig, want)


def check_password(username: str, password: str) -> bool:
    user, expected = web_credentials()
    if not expected:
        return False
    ok_user = secrets.compare_digest((username or "").strip(), user)
    ok_pass = secrets.compare_digest(password or "", expected)
    return ok_user and ok_pass


def authorized(request: Request,
               credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Cookie 会话、Bearer token、Basic 任一通过即可。只读 token 不能改状态。"""
    url = getattr(request, "url", None)
    if getattr(url, "path", "") in ANON_PATHS:
        return

    settings = load_settings()
    token = _bearer(request)
    if token and settings.api_token and secrets.compare_digest(token, settings.api_token):
        if settings.api_token_readonly and request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(403, "该 API token 是只读的，不允许执行写操作")
        return

    if verify_session(request.cookies.get(SESSION_COOKIE, "")):
        return

    user, password = web_credentials()
    if not password:
        return
    # Basic 只在调用方主动带了 Basic 头时才校验，绝不主动发 WWW-Authenticate
    if credentials is not None \
            and secrets.compare_digest(credentials.password, password) \
            and secrets.compare_digest(credentials.username, user):
        return

    raise NeedLogin()


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
    add_event(0, "已生成 AI 接入令牌（给内网 Agent 用）", "info")
    return settings.api_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.umask(0o077)
    user, password = web_credentials()
    source = web_password_source()
    if source == "new":
        bar = "=" * 66
        print(f"\n{bar}\n"
              f"  首次启动，已自动生成 Web 登录口令（以后重启不变）\n"
              f"  用户名：{user}\n"
              f"  密  码：{password}\n"
              f"  已写入：{ACCOUNT_FILE}（权限 600）\n"
              f"  想换口令：登录后在「设置 → 登录账号」里改；"
              f"也可设 WEB_PASSWORD 环境变量后重启\n"
              f"{bar}\n", flush=True)
    elif source == "memory":
        print(f"\n[警告] 数据目录不可写，本次登录口令仅存在于内存：\n"
              f"  用户名：{user}  密码：{password}\n", flush=True)
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


app = FastAPI(title="JAV-Subtitle", lifespan=lifespan,
              dependencies=[Depends(authorized)])


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    """页面上没人登录 → 跳登录页；接口上没人登录 → 回 401 JSON（不带 Basic 挑战）。"""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "未登录", "login": "/login"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


class AccountBody(BaseModel):
    username: str = ""
    password: str = ""
    current_password: str = ""


@app.get("/api/account")
def get_account():
    """当前登录账号。口令永远不回传，只说从哪来、能不能改。"""
    user, _ = web_credentials()
    return {
        "username": user,
        "source": web_password_source(),
        "locked_reason": account_locked_reason(),
        "editable": not account_locked_reason(),
        "min_password_len": MIN_PASSWORD_LEN,
    }


@app.post("/api/account")
def set_account(body: AccountBody, request: Request):
    """改登录账号与口令。已登录即可改，不要求再验一次旧口令。

    页面上只留「用户名 + 新口令」两栏，少一道确认少一次卡壳。
    旧的 current_password 字段仍然接受：带上就校验，带错照样拒绝。
    """
    user, password = web_credentials()
    if body.current_password and not secrets.compare_digest(body.current_password, password):
        time.sleep(0.4)
        raise HTTPException(400, "当前口令不对")
    target_user = (body.username or "").strip() or user
    target_pass = body.password or ""
    if not target_pass:
        target_pass = password       # 只改用户名时口令保持不动
    try:
        update_credentials(target_user, target_pass)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = JSONResponse({"ok": True, "username": target_user,
                             "relogin": False})
    # 会话签名由口令派生，改完旧签名就失效了；这里当场换一张新票
    response.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", path="/")
    return response


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


# logo / favicon 是静态图，不走鉴权也要能取（登录页要用），缓存一天
_ICONS = {"logo.png": "logo.png", "favicon.png": "favicon.png",
          "apple-touch-icon.png": "apple-touch-icon.png"}


@app.get("/static/{name}")
def static_icon(name: str):
    if name not in _ICONS:
        raise HTTPException(404, "没有这个文件")
    return FileResponse(STATIC_DIR / _ICONS[name],
                        headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/login")
def do_login(body: LoginBody):
    if not check_password(body.username, body.password):
        time.sleep(0.4)          # 拖一下，别让人拿脚本硬刷口令
        raise HTTPException(401, "用户名或口令不对")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, make_session(),
        max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
    )
    return response


@app.post("/api/logout")
def do_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response



class SettingsPatch(BaseModel):
    media_root: str | None = None
    source_language: str | None = None
    target_language: str | None = None
    whisper_model: str | None = None
    cpu_threads: int | None = None
    beam_size: int | None = None
    asr_word_timestamps: bool | None = None
    asr_split_gap: float | None = None
    asr_max_cue_seconds: float | None = None
    asr_max_cue_chars: int | None = None
    asr_batch_size: int | None = None
    extract_workers: int | None = None
    asr_workers: int | None = None
    translate_workers: int | None = None
    translate_rpm: int | None = None
    translate_min_interval: float | None = None
    translate_429_backoff: int | None = None
    translate_provider: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    google_api_key: str | None = None
    deepl_api_key: str | None = None
    deepl_api_url: str | None = None
    translate_fallback: str | None = None
    translation_batch_size: int | None = None
    translate_retries: int | None = None
    daily_char_budget: int | None = None
    tm_enabled: bool | None = None
    scrape_enabled: bool | None = None
    scrape_providers: str | None = None
    scrape_prefer: str | None = None
    scrape_min_interval: float | None = None
    scrape_hourly_limit: int | None = None
    subdl_api_key: str | None = None
    opensubtitles_api_key: str | None = None
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


@app.get("/api/settings/defaults")
def get_settings_defaults():
    """设置页「重置」用：程序内置默认值。不落库，只回给前端填表。"""
    return Settings().safe_dict()


@app.put("/api/settings")
def set_settings(patch: SettingsPatch):
    current = load_settings()
    data = current.safe_dict()
    changes = patch.model_dump(exclude_none=True)
    secret_fields = ("openai_api_key", "google_api_key", "deepl_api_key",
                     "subdl_api_key", "opensubtitles_api_key")
    for key, value in changes.items():
        if key in secret_fields:
            # 页面回传的掩码（全是星号）不能落库，否则之后真请求就是 403
            if isinstance(value, str) and value and set(value) == {"*"}:
                continue
            # 从聊天/网页粘进来的 key 常带引号、空格、甚至 "Bearer " 前缀，
            # 原样塞进请求头会被上游判成无效 token，这里统一收拾干净。
            value = clean_secret(value)
        if key == "translate_provider":
            value = normalize_provider(value)
        if key == "scrape_providers":
            # 界面上已经没有这个选项了，老库里存的 kitsunekko/subdl 之类顺手收敛掉
            value = "subtitlecat"
        if key == "translate_fallback":
            # 顺手去重、去掉主渠道自己，存成规范的 "google,bing"
            value = ",".join(p for p in parse_fallback(value)
                             if p != normalize_provider(patch.translate_provider
                                                       or data.get("translate_provider", "")))
        data[key] = value
    # 刮削平台界面上已不可选：无论这次有没有提交这个字段，库里都收敛成 subtitlecat
    data["scrape_providers"] = "subtitlecat"
    # 刮削是必经步骤，界面也不给开关，老库里存的 false 一并纠正
    data["scrape_enabled"] = True
    updated = Settings.from_dict(data)
    save_settings(updated)
    # 模型或线程数变了就把已加载的实例丢掉，否则旧实例会一直沿用
    if any(k in changes for k in ("cpu_threads", "whisper_model")):
        asr.clear_cache()
    pipeline.invalidate_settings_cache()
    reschedule()
    return updated.safe_dict()


@app.get("/api/device")
def get_device():
    """转写固定用 CPU，这里只回线程数给界面显示。"""
    return pipeline.device_summary(load_settings())


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


@app.get("/api/translate/providers")
def translate_providers():
    """给设置页下拉用：有哪几个渠道、各自的说明。"""
    settings = load_settings()
    return {
        "current": normalize_provider(settings.translate_provider),
        "items": [{"id": p, "label": PROVIDER_LABELS[p],
                   "needs_key": p in ("openai", "deepl")} for p in PROVIDERS],
    }


class TranslateTestBody(BaseModel):
    provider: str | None = None
    text: str | None = None
    # 设置页表单里还没点保存的值。测试要测「你现在在页面上选的渠道 + 你现在填的 key」，
    # 而不是上一次保存进库的那套 —— 否则选了 openai 却回 deepl 的结果（真发生过）。
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    google_api_key: str | None = None
    deepl_api_key: str | None = None
    deepl_api_url: str | None = None


_TEST_OVERRIDES = ("openai_base_url", "openai_api_key", "openai_model",
                   "google_api_key", "deepl_api_key", "deepl_api_url")


def _merge_test_overrides(settings, body: TranslateTestBody):
    """把表单里填了的值盖到已保存的设置上。

    空字符串 / 掩码 ******** 一律视为「没填」，继续用库里存的，
    免得用户只改了个模型名就把已经填好的 key 抹成空。
    """
    changes: dict = {}
    for key in _TEST_OVERRIDES:
        value = getattr(body, key, None)
        if value is None:
            continue
        value = value.strip()
        if not value or value == "********":
            continue
        changes[key] = clean_secret(value) if key.endswith("_api_key") else value
    return replace(settings, **changes) if changes else settings


@app.post("/api/translate/test")
def translate_test(body: TranslateTestBody | None = None):
    """拿指定（或当前）渠道真发一句，把上游原话回给界面。

    这就是排查「key 在别处能用、到这儿 401」最直接的办法：
    看到的错误是上游返回的原文，不是我加工过的猜测。
    """
    body = body or TranslateTestBody()
    settings = _merge_test_overrides(load_settings(), body)
    provider = normalize_provider(body.provider or settings.translate_provider)
    # 「测试这一档」就只测这一档：把降级链关掉。
    # 否则 deepl 的 key 是空的，链子悄悄落到必应，页面上却显示「deepl 正常」——
    # 那是骗人的（用户就撞到过：选 openai 却回 deepl 的结果）。
    settings = replace(settings, translate_fallback="")
    text = (body.text or "こんにちは、今日はいい天気ですね。").strip() or "こんにちは"
    started = time.time()
    engine = build_translator(provider, settings)
    diag: dict = {}
    # DeepL 的 403 几乎全是 key 本身的问题：把 key 的形状和当月额度一并带回去，
    # 用户一眼能看出「是没粘全 / 少了 :fx / 订阅没生效 / 额度用光」中的哪一种。
    # 压根没填 key 的时候不往界面上堆这堆诊断——错误信息已经说清「没填密钥」了。
    has_key = {"deepl": bool(settings.deepl_api_key),
               "openai": bool(settings.openai_api_key)}.get(provider, True)
    if has_key and engine.key_report():
        diag["key"] = engine.key_report()
        try:
            diag["usage"] = engine.usage()
        except Exception as exc:  # noqa: BLE001 - 诊断信息拿不到不影响主结果
            diag["usage"] = {"ok": False, "detail": str(exc)[:120]}
    try:
        outputs = engine.translate_texts([text])
        return {"ok": True, "provider": provider,
                "source": text, "target": outputs[0] if outputs else "",
                "seconds": round(time.time() - started, 2), **diag}
    except Exception as exc:  # noqa: BLE001 - 要把上游原话原样带回界面
        return {"ok": False, "provider": provider, "source": text,
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - started, 2), **diag}
    finally:
        engine.close()


@app.post("/api/queue/reset-limiter")
def reset_limiter():
    limiter.reset()
    return {"ok": True}


# ------------------------------------------------------------------ cleanup


class CleanBody(BaseModel):
    scope: str = ""              # 空 = 整个媒体根目录
    items: list[str] = []        # jobs / index / subtitles / work / audio / tm
    confirm: bool = False        # 必须显式 true 才真删


@app.get("/api/clean/preview")
def clean_preview(scope: str = "", items: str = ""):
    """先看会删多少，不动手。items 用逗号分隔，空表示全部。"""
    settings = load_settings()
    wanted = [i.strip() for i in items.split(",") if i.strip()] or None
    try:
        return cleanup.preview(settings.media_root, scope,
                               settings.target_language, wanted)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/clean/usage")
def clean_usage():
    return cleanup.disk_usage()


@app.post("/api/clean")
def clean_run(body: CleanBody):
    """执行清理。没带 confirm=true 直接 400 —— 这个接口是真删文件，不能误触。"""
    if not body.confirm:
        raise HTTPException(400, "需要 confirm=true 才会执行清理")
    if not body.items:
        raise HTTPException(400, "items 不能为空")
    known = [i for i in body.items if i in cleanup.ITEMS]
    if not known:
        raise HTTPException(400, f"不认识要清理的项：{body.items}")
    settings = load_settings()
    try:
        result = cleanup.clean(settings.media_root, body.scope,
                               settings.target_language, known)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


# ------------------------------------------------------------------ scraping


class ScrapeTestBody(BaseModel):
    name: str = ""            # 影片文件名或番号，留空用设置里第一个视频


@app.get("/api/scrape/status")
def scrape_status():
    """刮削现在能不能用、限速到什么程度。"""
    settings = load_settings()
    return {"providers": scraping.provider_status(settings),
            "order": scraping.active_order(settings.scrape_providers),
            "enabled": True,
            "interval": settings.scrape_min_interval,
            "hourly_limit": settings.scrape_hourly_limit,
            "gate": scraping.GATE.snapshot()}


@app.post("/api/scrape/test")
def scrape_test(body: ScrapeTestBody):
    """拿一个片名真跑一遍刮削，把结果原样返回。"""
    settings = load_settings()
    name = body.name.strip()
    if not name:
        media_root = Path(settings.media_root).expanduser().resolve()
        for current, _dirs, files in os.walk(media_root):
            for candidate in files:
                if candidate.lower().endswith((".mp4", ".mkv", ".avi", ".wmv", ".ts", ".mov")):
                    name = candidate
                    break
            if name:
                break
    if not name:
        raise HTTPException(400, "没有可测试的文件名，请手动填一个")
    probe = Settings.from_dict({**settings.safe_dict(), "scrape_enabled": True})
    # wait=False：按钮点一下不该卡二十秒。间隔没到就直接把「还要等几秒」回给界面。
    result = scraping.scrape(name, probe, wait=False)
    return {"name": name, "ok": result.ok, "provider": result.provider,
            "language": result.language, "cues": len(result.cues),
            "source": result.source_name, "tried": result.tried,
            "notes": result.notes}


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
        {"method": "GET", "path": "/api/translate/providers", "desc": "可选翻译渠道列表"},
        {"method": "POST", "path": "/api/translate/test", "body": {"text": "..."}, "desc": "用当前渠道真发一句，返回译文或上游原始报错"},
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
    # 新的在上：直接按 id 倒序返回，前端不再翻转
    return [dict(r) for r in rows]


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
    add_event(0, "AI 接入令牌已重置，旧的立即失效", "warn")
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
    return {"ok": True, "version": "3.10"}
