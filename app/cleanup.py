"""清理与重置。

用户会在跑了半截、想改配置重来的时候卡住：没有地方删任务、删索引、删跑坏的字幕。
这里把所有「重来」要用到的动作集中起来，全部走同一个入口，且**必须先预览再执行**。

铁律：
- 只删数据库记录、工作目录里的中间文件、以及媒体目录里的 `.srt` 字幕；
- 永远不碰原视频（扩展名白名单 + 后缀检查双重拦）；
- 删除范围一律限制在媒体根目录内，路径逃逸直接报错。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import AUDIO_DIR, DATA_DIR, VIDEO_EXTENSIONS
from .db import add_event, connect
from .pipeline import WORK_DIR
from .subtitles import assert_subtitle_path

# 可清理的项。顺序就是 UI 上的顺序。
ITEMS = ("jobs", "index", "subtitles", "work", "audio", "tm")


def _subtitle_kinds(target_language: str) -> tuple[str, ...]:
    lang = (target_language or "zh-CN").replace("_", "-")
    return (f".{lang}.srt", ".ja.srt")


def _scope(media_root: str, scope: str) -> Path:
    """把 scope 解析成绝对路径，并保证它落在媒体根目录内。"""
    root = Path(media_root).expanduser().resolve()
    if not scope:
        return root
    path = Path(scope).expanduser().resolve()
    if path != root and not path.is_relative_to(root):
        raise ValueError("目录不在媒体根目录内")
    return path


def _like(scope_path: Path, root: Path) -> tuple[str, tuple[Any, ...]]:
    """生成 media/jobs 的 ``path`` 前缀条件。scope 就是根目录时不加限制。"""
    if scope_path == root:
        return "1=1", ()
    prefix = str(scope_path).rstrip(os.sep) + os.sep
    return "(path = ? OR path LIKE ?)", (str(scope_path), prefix + "%")


def _job_where(scope_path: Path, root: Path) -> tuple[str, tuple[Any, ...]]:
    if scope_path == root:
        return "1=1", ()
    prefix = str(scope_path).rstrip(os.sep) + os.sep
    return "(video_path = ? OR video_path LIKE ?)", (str(scope_path), prefix + "%")


def _iter_subtitles(scope_path: Path, kinds: tuple[str, ...]):
    if not scope_path.is_dir():
        return
    for current, dirs, files in os.walk(scope_path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {"@eaDir", "#recycle", ".Trash"}]
        for name in files:
            if name.endswith(kinds):
                yield Path(current) / name


def _dir_stat(path: Path) -> tuple[int, int]:
    """(文件数, 字节数)"""
    if not path.exists():
        return 0, 0
    files = 0
    size = 0
    for current, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                size += (Path(current) / name).stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def preview(media_root: str, scope: str = "", target_language: str = "zh-CN",
            items: list[str] | None = None) -> dict[str, Any]:
    """先算清楚每一项会删多少，不执行任何删除。"""
    root = Path(media_root).expanduser().resolve()
    scope_path = _scope(media_root, scope)
    kinds = _subtitle_kinds(target_language)
    wanted = [i for i in (items or ITEMS) if i in ITEMS]

    where_media, params_media = _like(scope_path, root)
    where_job, params_job = _job_where(scope_path, root)

    out: dict[str, Any] = {"scope": str(scope_path), "root": str(root),
                           "media_root": str(root), "items": {}}

    if "jobs" in wanted:
        with connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE {where_job}", params_job).fetchone()
            per = conn.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs WHERE {where_job} GROUP BY status",
                params_job).fetchall()
        out["items"]["jobs"] = {"count": row["n"],
                                "by_status": {r["status"]: r["n"] for r in per}}

    if "index" in wanted:
        with connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM media WHERE {where_media}", params_media).fetchone()
        out["items"]["index"] = {"count": row["n"]}

    if "subtitles" in wanted:
        found = list(_iter_subtitles(scope_path, kinds))
        out["items"]["subtitles"] = {
            "count": len(found),
            "bytes": sum(p.stat().st_size for p in found if p.exists()),
            "suffixes": list(kinds),
            "samples": [str(p) for p in found[:5]],
        }

    if "work" in wanted:
        n, b = _dir_stat(WORK_DIR)
        out["items"]["work"] = {"count": n, "bytes": b, "path": str(WORK_DIR)}

    if "audio" in wanted:
        n, b = _dir_stat(AUDIO_DIR)
        out["items"]["audio"] = {"count": n, "bytes": b, "path": str(AUDIO_DIR)}

    if "tm" in wanted:
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM tm").fetchone()
        out["items"]["tm"] = {"count": row["n"]}

    return out


def _unlink_all(paths) -> tuple[int, list[str]]:
    removed = 0
    errors: list[str] = []
    for path in paths:
        try:
            assert_subtitle_path(path)          # 后缀白名单，非 .srt 一律拒绝
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except Exception as exc:                # noqa: BLE001 - 单个文件失败不该中断整批
            errors.append(f"{path}: {exc}")
    return removed, errors


def clean(media_root: str, scope: str = "", target_language: str = "zh-CN",
          items: list[str] | None = None, also_remove_video_lock: bool = True) -> dict[str, Any]:
    """执行清理。返回每一项实际删掉的数量。

    调用方应先调 :func:`preview` 让用户确认。
    """
    root = Path(media_root).expanduser().resolve()
    scope_path = _scope(media_root, scope)
    kinds = _subtitle_kinds(target_language)
    wanted = [i for i in (items or ITEMS) if i in ITEMS]
    result: dict[str, Any] = {"scope": str(scope_path), "items": {},
                              "errors": []}

    where_media, params_media = _like(scope_path, root)
    where_job, params_job = _job_where(scope_path, root)

    if "jobs" in wanted:
        with connect() as conn:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM jobs WHERE {where_job}", params_job).fetchall()]
            if ids:
                marks = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM events WHERE job_id IN ({marks})", ids)
                conn.execute(f"DELETE FROM jobs WHERE id IN ({marks})", ids)
        result["items"]["jobs"] = len(ids)

    if "index" in wanted:
        with connect() as conn:
            cur = conn.execute(f"DELETE FROM media WHERE {where_media}", params_media)
            result["items"]["index"] = cur.rowcount

    if "subtitles" in wanted:
        found = list(_iter_subtitles(scope_path, kinds))
        removed, errors = _unlink_all(found)
        result["items"]["subtitles"] = removed
        result["errors"].extend(errors)

    if "work" in wanted:
        n, _b = _dir_stat(WORK_DIR)
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        result["items"]["work"] = n

    if "audio" in wanted:
        n, _b = _dir_stat(AUDIO_DIR)
        shutil.rmtree(AUDIO_DIR, ignore_errors=True)
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        result["items"]["audio"] = n

    if "tm" in wanted:
        with connect() as conn:
            cur = conn.execute("DELETE FROM tm")
            result["items"]["tm"] = cur.rowcount

    label = " / ".join(f"{k} {v}" for k, v in result["items"].items())
    add_event(0, f"清理完成（范围 {scope_path}）：{label or '无可清理项'}", "warn")
    return result


def disk_usage() -> dict[str, Any]:
    """数据目录占了多少，给设置页一个直观数字。"""
    out: dict[str, Any] = {}
    for key, path in (("data", DATA_DIR), ("work", WORK_DIR), ("audio", AUDIO_DIR)):
        n, b = _dir_stat(path)
        out[key] = {"path": str(path), "files": n, "bytes": b}
    return out
