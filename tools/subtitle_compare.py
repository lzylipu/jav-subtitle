#!/usr/bin/env python3
"""对比两份字幕的时间轴，不比较翻译文字。

用途：拿站点现成字幕当参考，和本项目 ASR 转写出来的日文字幕逐项对比，
回答「时间轴差在哪、漏了多少、虚挂了多少」。

用法：
    python tools/subtitle_compare.py 参考.srt 转写.srt
    python tools/subtitle_compare.py 参考.srt 转写.srt --json report.json

输出指标
--------
cues            两份字幕各有多少条
span_s          字幕覆盖的时间跨度（最后一条结束时间）
speech_s        所有字幕条目的时长之和
coverage        speech_s / span_s，字幕在时间轴上的占用率
median/p95/max  单条字幕时长分布
over_8s         超过 8 秒的「虚挂」长条数量
gaps_gt_2s      相邻两条之间超过 2 秒的空档数量与总时长
overlaps        时间轴互相重叠的条数
start_offset    转写条与参考条按时间最近配对后的起点偏移中位数 / P95

时间轴比较只做统计，不改任何文件。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_left
from dataclasses import asdict, dataclass
from pathlib import Path

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


@dataclass
class Cue:
    start: float
    end: float
    text: str


def parse_srt(path: Path) -> list[Cue]:
    """宽松解析 SRT：只要能匹配到时间轴就收，坏块跳过，不让一个坏行毁掉整份。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 2:
            continue
        index = 0
        if lines[0].strip().isdigit():
            index = int(lines[0].strip())
            lines = lines[1:]
        if not lines:
            continue
        m = TIME_RE.search(lines[0].strip())
        if not m:
            continue
        start = _seconds(*m.group(1, 2, 3, 4))
        end = _seconds(*m.group(5, 6, 7, 8))
        if end < start:
            start, end = end, start
        body = "\n".join(lines[1:]).strip()
        cues.append(Cue(start, end, body))
    return cues


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(ordered[pos], 3)


def text_style(cues: list[Cue]) -> dict:
    """描述字幕的书写习惯，而不是具体文案。

    人工字幕和 ASR 输出最容易被忽略的差异不只在时间轴，还在“每条写多长、
    一次放几行、开头留多久、末尾留多久”。这些指标可以在不同影片之间比较。
    """
    lines = [line for cue in cues for line in cue.text.splitlines() if line.strip()]
    lengths = [len(line.strip()) for line in lines]
    per_cue_lines = [len([line for line in cue.text.splitlines() if line.strip()])
                     for cue in cues]
    last_end = max((cue.end for cue in cues), default=0.0)
    return {
        "text_lines": len(lines),
        "lines_per_cue_median": _percentile([float(x) for x in per_cue_lines], 0.5),
        "lines_per_cue_p95": _percentile([float(x) for x in per_cue_lines], 0.95),
        "line_chars_median": _percentile([float(x) for x in lengths], 0.5),
        "line_chars_p95": _percentile([float(x) for x in lengths], 0.95),
        "max_line_chars": max(lengths, default=0),
        "leading_gap_s": round(cues[0].start, 3) if cues else 0.0,
        "tail_margin_s": round(last_end, 3),
    }


def describe(cues: list[Cue]) -> dict:
    durations = [round(c.end - c.start, 3) for c in cues]
    span = round(max((c.end for c in cues), default=0.0), 3)
    speech = round(sum(durations), 3)
    gaps = []
    for prev, nxt in zip(cues, cues[1:]):
        gap = nxt.start - prev.end
        if gap > 2.0:
            gaps.append(round(gap, 3))
    overlaps = 0
    for prev, nxt in zip(cues, cues[1:]):
        if nxt.start < prev.end - 0.05:
            overlaps += 1
    return {
        "cues": len(cues),
        "span_s": span,
        "speech_s": speech,
        "coverage": round(speech / span, 4) if span else 0.0,
        "median_cue_s": _percentile(durations, 0.5),
        "p95_cue_s": _percentile(durations, 0.95),
        "max_cue_s": round(max(durations, default=0.0), 3),
        "over_8s": sum(1 for d in durations if d > 8.0),
        "over_12s": sum(1 for d in durations if d > 12.0),
        "gaps_gt_2s": len(gaps),
        "gap_seconds_gt_2s": round(sum(gaps), 3),
        "overlaps": overlaps,
        "style": text_style(cues),
    }


def start_offsets(reference: list[Cue], produced: list[Cue]) -> list[float]:
    """按起点就近配对，返回两条字幕的起点差（正数=转写更晚）。

    对每一条待比较字幕，找参考字幕里起点最近的那条作为配对。
    用「就近」而不是顺序硬配：两条字幕切句粒度不同，同一句话可能被切成
    1 条或 3 条，硬配会把「切片边界」误算成「时间轴偏移」。
    就近配对统计出来的就是真正的时间偏移。
    """
    if not reference or not produced:
        return []
    ref_starts = [c.start for c in reference]
    offsets: list[float] = []
    for cue in produced:
        pos = bisect_left(ref_starts, cue.start)
        candidates = []
        if pos < len(ref_starts):
            candidates.append(ref_starts[pos])
        if pos > 0:
            candidates.append(ref_starts[pos - 1])
        nearest = min(candidates, key=lambda x: abs(x - cue.start))
        offsets.append(round(cue.start - nearest, 3))
    return offsets


def compare(reference: list[Cue], produced: list[Cue]) -> dict:
    offsets = start_offsets(reference, produced)
    return {
        "reference": describe(reference),
        "produced": describe(produced),
        "alignment": {
            "paired": len(offsets),
            "start_offset_median_s": _percentile(offsets, 0.5),
            "start_offset_p95_s": _percentile(offsets, 0.95),
            "start_offset_abs_median_s": _percentile([abs(x) for x in offsets], 0.5),
            "within_1s_ratio": (round(sum(1 for x in offsets if abs(x) <= 1.0) / len(offsets), 4)
                                if offsets else 0.0),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference", help="参考字幕（SRT）")
    ap.add_argument("produced", help="待比较字幕（SRT）")
    ap.add_argument("--json", help="把结果写成 JSON 文件")
    args = ap.parse_args()

    ref = parse_srt(Path(args.reference))
    out = parse_srt(Path(args.produced))
    if not ref or not out:
        print(f"解析失败：参考 {len(ref)} 条，待比较 {len(out)} 条", file=sys.stderr)
        return 2

    report = compare(ref, out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
