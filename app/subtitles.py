"""SRT parsing, rendering and atomic writing."""
from __future__ import annotations

import math
import os
import re
import tempfile
import unicodedata
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


# 人工字幕的行宽惯例：日语/中文一屏最多两行，每行约 20 个全角字
# （40 个西文列宽）。参考字幕 NPS-476 的实测上限就是 20 个日文字/行、
# 两行/条；本项目此前把整句塞进一行，最长到 42 个日文字（84 列），
# 在电视/手机上会被播放器强制截断。
SUBTITLE_MAX_COLUMNS = 40
SUBTITLE_MAX_LINES = 2

_BREAK_AFTER = "、。，．！？!?…；： "
_BREAK_BEFORE_BAD = "、。，．！？!?…；：」』）】〉》"


def display_width(text: str) -> int:
    """按显示列宽计数：全角/宽字符算 2，其余算 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _break_index(text: str, target: int) -> int:
    """在 target 列附近找最自然的断行位置。

    优先断在标点/空格之后；不允许断在标点之前；尽量不把西文单词劈开。
    找不到合适的点就退回按列宽硬断。
    """
    if len(text) <= 1:
        return 1
    best_index = 0
    best_score = None
    total = display_width(text)
    for index in range(1, len(text)):
        left = display_width(text[:index])
        if left <= 0 or left >= total:
            continue
        # 别把标点甩到下一行开头
        if text[index] in _BREAK_BEFORE_BAD:
            continue
        score = abs(left - target)
        if text[index - 1] in _BREAK_AFTER:
            score -= 3
        if text[index].isspace():
            score -= 2
        # 西文单词中间不加断点
        if (text[index - 1].isascii() and text[index - 1].isalnum()
                and text[index].isascii() and text[index].isalnum()):
            score += 12
        if best_score is None or score < best_score:
            best_score = score
            best_index = index
    return best_index or max(1, min(len(text) - 1, target))


def wrap_text(text: str, max_columns: int = SUBTITLE_MAX_COLUMNS,
              max_lines: int = SUBTITLE_MAX_LINES) -> str:
    """把单行字幕排成两行，给电视/手机播放器留出安全行宽。

    - 已经手动换行的文本原样保留（刮削字幕的排版不重排）；
    - 短句不折行；
    - 超长句按总宽度均分，最多 ``max_lines`` 行；两行装不下才继续加行，
      绝不截断文字。
    """
    if not text or "\n" in text:
        return text
    total = display_width(text)
    if total <= max_columns:
        return text
    line_count = max_lines if max_lines > 1 else 1
    # 两行能装下时优先两行；实在装不下再按每行 max_columns 增加行数。
    if total <= max_columns * max_lines + 8:
        line_count = max_lines
    else:
        line_count = max(line_count, math.ceil(total / max_columns))
    target = max(1, math.ceil(total / line_count))
    lines: list[str] = []
    rest = text
    for _ in range(line_count - 1):
        if display_width(rest) <= max_columns and not lines:
            break
        index = _break_index(rest, target)
        left, right = rest[:index].rstrip(), rest[index:].lstrip()
        if not left or not right:
            break
        lines.append(left)
        rest = right
        # 下一行按剩余宽度重新均分
        target = max(1, math.ceil(display_width(rest) / max(1, line_count - len(lines))))
    if rest:
        lines.append(rest)
    return "\n".join(lines)


def format_cues(cues: list[Cue]) -> list[Cue]:
    """只改排版，不动编号和时间轴。"""
    return [Cue(c.number, c.start, c.end, wrap_text(c.text)) for c in cues]


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
