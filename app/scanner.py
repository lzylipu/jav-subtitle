"""只读递归索引：扫描媒体目录，并按目录汇总视频数量。

v3 新增「本地索引」能力：
- 扫描一次就把所有视频落进 media 表；
- 之后可以在 Web UI 按目录浏览「这个文件夹里有多少个视频、多少已完成、多少还没做」；
- 也可以只对选中的目录下加入队，不必整库一起跑。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from .config import VIDEO_EXTENSIONS
from .db import connect, now

SKIP_DIRS = {".git", "@eaDir", "#recycle", ".Trash", "$RECYCLE.BIN", ".stfolder",
             "__pycache__", "node_modules", ".DS_Store"}


def iter_videos(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(current) / d).is_symlink()]
        for name in files:
            video = Path(current) / name
            if video.suffix.lower() in VIDEO_EXTENSIONS and not video.is_symlink():
                yield video


def scan_media(root: str) -> dict[str, int]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"媒体目录不存在: {root_path}")
    discovered = updated = 0
    with connect() as conn:
        for video in iter_videos(root_path):
            try:
                stat = video.stat()
            except OSError:
                continue
            path = str(video)
            row = conn.execute("SELECT size,mtime FROM media WHERE path=?", (path,)).fetchone()
            stamp = now()
            if row:
                if row["size"] != stat.st_size or row["mtime"] != stat.st_mtime:
                    conn.execute(
                        "UPDATE media SET size=?,mtime=?,updated_at=?,stable_scans=0 WHERE path=?",
                        (stat.st_size, stat.st_mtime, stamp, path),
                    )
                else:
                    # 只累加稳定次数，不动 updated_at：updated_at 语义是「上次内容变化时间」，
                    # 每次扫描都刷新会让 120 秒稳定闸门永远不通过。
                    conn.execute(
                        "UPDATE media SET stable_scans=stable_scans+1 WHERE path=?", (path,)
                    )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO media(path,size,mtime,discovered_at,updated_at) VALUES(?,?,?,?,?)",
                    (path, stat.st_size, stat.st_mtime, stamp, stamp),
                )
                discovered += 1
    return {"discovered": discovered, "updated": updated, "total": discovered + updated}


def indexed_media(limit: int = 0) -> list[dict]:
    sql = "SELECT * FROM media ORDER BY path"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def _blank() -> dict[str, int]:
    return {"videos": 0, "done": 0, "running": 0, "pending": 0, "failed": 0, "new": 0}


def library_tree(root: str, path: str = "", limit_children: int = 500) -> dict[str, Any]:
    """按目录汇总视频数量。

    返回 `path` 这一层的直接子目录，每个子目录带「含子目录在内」的递归计数，
    外加当前目录直属视频的计数。视频只从 media 表取，所以必须先扫描（索引）。
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"媒体目录不存在: {root_path}")
    current = (Path(path).expanduser().resolve() if path else root_path)
    try:
        if not current.is_relative_to(root_path):
            raise ValueError("目录不在媒体根目录内")
    except OSError as exc:  # pragma: no cover
        raise ValueError(f"无法解析目录: {exc}") from exc
    if not current.is_dir():
        raise FileNotFoundError(f"目录不存在: {current}")

    with connect() as conn:
        rows = conn.execute(
            "SELECT m.path AS path, j.status AS status FROM media m "
            "LEFT JOIN jobs j ON j.video_path = m.path"
        ).fetchall()

    counts: dict[str, dict[str, int]] = {}   # 含所有子目录的递归计数
    direct: dict[str, dict[str, int]] = {}   # 只算本层直属视频

    for row in rows:
        video = Path(row["path"])
        try:
            folder = video.parent.resolve()
        except OSError:
            continue
        if not _is_within(folder, root_path):
            continue
        status = row["status"]
        bucket = ("new" if status is None else
                  "done" if status in ("done", "skipped") else
                  "failed" if status == "failed" else
                  "running" if status == "running" else "pending")

        here = direct.setdefault(str(folder), _blank())
        here["videos"] += 1
        here[bucket] += 1

        # 先算自己，再一路往上累加到根目录
        node = folder
        while True:
            agg = counts.setdefault(str(node), _blank())
            agg["videos"] += 1
            agg[bucket] += 1
            if node == root_path:
                break
            parent_node = node.parent
            if parent_node == node:   # 根目录保护
                break
            node = parent_node

    children: list[dict[str, Any]] = []
    try:
        entries = sorted([p for p in current.iterdir() if p.is_dir()],
                         key=lambda p: p.name.lower())
    except OSError as exc:
        raise FileNotFoundError(f"无法读取目录: {exc}") from exc
    for entry in entries[:limit_children]:
        if entry.name in SKIP_DIRS or entry.is_symlink():
            continue
        stats = counts.get(str(entry), _blank())
        if stats["videos"] == 0:
            continue
        children.append({"name": entry.name, "path": str(entry), **stats})

    self_stats = counts.get(str(current), _blank())
    direct_stats = direct.get(str(current), _blank())
    return {
        "root": str(root_path),
        "path": str(current),
        "parent": str(current.parent) if current != root_path else "",
        "depth": len(current.relative_to(root_path).parts) if current != root_path else 0,
        "self": {**self_stats, "direct": direct_stats["videos"]},
        "children": children,
        "children_total": len(children),
        "library_total": counts.get(str(root_path), _blank()),
    }


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child == parent or child.is_relative_to(parent)
    except (OSError, ValueError):
        return False
