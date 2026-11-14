"""SRT parsing, rendering and atomic writing."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIME_RE = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})")


@dataclass
class Cue:
    number: int
    start: str
    end: str
    text: str


def parse_srt(content: str) -> list[Cue]:
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.splitlines()
        if len(lines) < 2:
            raise ValueError("SRT 包含不完整的字幕片段")
        index = 0
        if lines[0].strip().isdigit():
            index = int(lines[0].strip())
            lines = lines[1:]
        if not lines:
            raise ValueError("SRT 缺少时间轴")
        match = TIME_RE.fullmatch(lines[0].strip())
        if not match or len(lines) < 2 or not "\n".join(lines[1:]).strip():
            raise ValueError("SRT 时间轴或正文无效")
        cues.append(Cue(index, match.group(1), match.group(2), "\n".join(lines[1:]).strip()))
    return cues


def render_srt(cues: list[Cue]) -> str:
    blocks = [f"{pos}\n{c.start} --> {c.end}\n{c.text}" for pos, c in enumerate(cues, 1)]
    return "\n\n".join(blocks) + "\n"


def target_paths(video: Path, target_language: str) -> tuple[Path, Path]:
    lang = target_language.replace("_", "-")
    return video.with_suffix(f".{lang}.srt"), video.with_suffix(".ja.srt")


def assert_subtitle_path(path: Path) -> None:
    """只允许写 .srt。原视频永远不该被这个函数碰到。"""
    if path.suffix.lower() != ".srt":
        raise ValueError(f"拒绝写入非字幕文件：{path}")


def atomic_write(path: Path, content: str, overwrite: bool = False) -> None:
    assert_subtitle_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        if overwrite:
            os.replace(name, path)
        else:
            os.link(name, path)
            os.unlink(name)
    finally:
        if os.path.exists(name):
            os.unlink(name)
