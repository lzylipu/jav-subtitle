"""SQLite storage: schema, settings, job queue, translation memory, counters."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH, Settings

BEIJING = timezone(timedelta(hours=8))  # 项目写死北京时间，跟容器 TZ 无关

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS media (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stable_scans INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_path TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'extract',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    audio_path TEXT NOT NULL DEFAULT '',
    ja_srt TEXT NOT NULL DEFAULT '',
    zh_srt TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    cached_chars INTEGER NOT NULL DEFAULT 0,
    detected_language TEXT NOT NULL DEFAULT '',
    asr_seconds REAL NOT NULL DEFAULT 0,
    translate_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, stage, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS tm (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);
"""


def now() -> str:
    # 固定北京时间（UTC+8），不依赖容器 TZ / 宿主机时区；日志和任务时间都按这个走
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """与旧版不同：这里显式 close，避免 2000 个任务把连接句柄耗光。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- settings


def load_settings() -> Settings:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='main'").fetchone()
    if not row:
        return Settings()
    return Settings.from_dict(json.loads(row["value"]))


def save_settings(settings: Settings) -> None:
    with _LOCK, connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('main',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (settings.to_json(),),
        )


# ---------------------------------------------------------------- events


def add_event(job_id: int, message: str, level: str = "info") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(job_id,created_at,level,message) VALUES(?,?,?,?)",
            (job_id, now(), level, message[:2000]),
        )


def job_events(job_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------- queue


def claim(stage: str) -> dict[str, Any] | None:
    """原子领取一个待处理任务。多线程/多进程都安全（单条 UPDATE 带子查询）。"""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' AND stage=? "
            "AND next_attempt_at<=? ORDER BY priority DESC, id LIMIT 1",
            (stage, datetime.now(timezone.utc).timestamp()),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status='running', updated_at=?, started_at=CASE WHEN started_at='' "
            "THEN ? ELSE started_at END WHERE id=?",
            (datetime.now(timezone.utc).timestamp(), now(), row["id"]),
        )
        data = dict(row)
    data["status"] = "running"
    return data


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).timestamp()
    keys = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), job_id))


STAGE_LABELS = {"extract": "抽音频", "asr": "转写", "translate": "翻译"}


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


def advance(job_id: int, stage: str, progress: int, message: str, **extra: Any) -> None:
    """把任务推进到下一阶段并重新置为 pending。"""
    payload = {
        "stage": stage,
        "status": "pending",
        "progress": progress,
        "message": message,
        "attempts": 0,
        "next_attempt_at": 0.0,
        **extra,
    }
    update_job(job_id, **payload)
    # 阶段切换写一条事件：日志页靠这个显示「进度走到哪一步了」
    add_event(job_id, f"进入「{stage_label(stage)}」阶段：{message}", "info")


STATUS_LABELS = {"done": "完成", "failed": "失败", "skipped": "跳过",
                 "pending": "待处理", "running": "处理中"}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def finish(job_id: int, stage: str, status: str = "done", message: str = "", **extra: Any) -> None:
    level = "info" if status == "done" else "warn"
    tail = (message or "").strip()
    # 日志给用户看，别中英混排：done/done 这种原样吐出来很怪
    # stage 有时传的是 "done" 这种终态而不是阶段名（pipeline 收尾就是这么调的），
    # 认不出是真阶段就别硬塞进文案里
    if stage in STAGE_LABELS:
        head = f"任务{status_label(status)}（{STAGE_LABELS[stage]}阶段）"
    else:
        head = f"任务{status_label(status)}"
    # message 常常就是「完成」两个字，跟状态重复，没必要写成「任务完成：完成」
    if not tail or tail == status_label(status):
        add_event(job_id, head, level)
    else:
        add_event(job_id, f"{head}：{tail}", level)
    update_job(
        job_id,
        stage=stage,
        status=status,
        progress=100,
        message=message,
        error="",
        finished_at=now(),
        **extra,
    )


def fail(job_id: int, error: str, stage: str, attempts: int, max_attempts: int, base_delay: float = 30.0) -> str:
    """失败处理：未超过上限则退避重排，超过则标记 failed。返回最终 status。"""
    message = error[:500]
    if attempts + 1 >= max_attempts:
        update_job(
            job_id,
            attempts=attempts + 1,
            status="failed",
            error=message,
            message=message,
            finished_at=now(),
        )
        add_event(job_id, f"[{stage}] 最终失败：{error[:800]}", "error")
        return "failed"
    delay = min(base_delay * (2 ** attempts), 3600)
    update_job(
        job_id,
        attempts=attempts + 1,
        status="pending",
        stage=stage,
        error=message,
        message=f"{message}（{int(delay)} 秒后重试）",
        next_attempt_at=datetime.now(timezone.utc).timestamp() + delay,
    )
    add_event(job_id, f"[{stage}] 第 {attempts + 1} 次失败，{int(delay)} 秒后重试：{error[:600]}", "warn")
    return "pending"


# ---------------------------------------------------------------- translation memory


def tm_lookup(texts: list[str], model: str) -> dict[str, str]:
    if not texts:
        return {}
    keys = [f"{model}\x00{t}" for t in texts]
    found: dict[str, str] = {}
    with connect() as conn:
        for chunk_start in range(0, len(keys), 400):
            chunk = keys[chunk_start:chunk_start + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT key,target FROM tm WHERE key IN ({marks})", chunk).fetchall()
            for row in rows:
                found[row["key"]] = row["target"]
    return found


def tm_store(pairs: list[tuple[str, str, str]], model: str) -> None:
    if not pairs:
        return
    stamp = now()
    with connect() as conn:
        conn.executemany(
            "INSERT INTO tm(key,source,target,created_at) VALUES(?,?,?,?) ON CONFLICT(key) DO NOTHING",
            [(f"{model}\x00{src}", src, tgt, stamp) for src, tgt in pairs],
        )


# ---------------------------------------------------------------- counters


def counter_add(key: str, value: float) -> float:
    with connect() as conn:
        conn.execute(
            "INSERT INTO counters(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
            (key, value),
        )
        row = conn.execute("SELECT value FROM counters WHERE key=?", (key,)).fetchone()
    return float(row["value"]) if row else value


def counter_get(key: str) -> float:
    with connect() as conn:
        row = conn.execute("SELECT value FROM counters WHERE key=?", (key,)).fetchone()
    return float(row["value"]) if row else 0.0
