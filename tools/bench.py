#!/usr/bin/env python3
"""实测这台机器能跑多快：转写 N 秒素材要多久。

用法（容器内）：
    python tools/bench.py /media/某部片子.mp4
    python tools/bench.py /media/某部片子.mp4 --model base --threads 4 --batch 8

输出「×实时」= 1 秒实际耗时能处理多少秒素材。用总素材时长除以它，就是预估工期。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import asr  # noqa: E402
from app.subtitles import Cue as SrtCue, format_cues, render_srt  # noqa: E402
from tools.subtitle_compare import Cue, describe  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--model", default=os.getenv("WHISPER_MODEL", "small"))
    ap.add_argument("--threads", type=int, default=0,
                    help="CPU 线程数，0 = 自动取 min(4, 核数)")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=0, help="只取前 N 秒，0 表示整片")
    ap.add_argument("--start", type=float, default=0, help="从第 N 秒开始截取")
    ap.add_argument("--stage", default="asr", choices=["asr", "extract", "full"],
                    help="asr=只要转写；extract=只要抽音频；full=抽音频+转写")
    ap.add_argument("--srt", help="把转写结果写成 SRT（配合 subtitle_compare 对比参考字幕）")
    args = ap.parse_args()

    threads = asr.threads_for(args.threads)

    video = Path(args.video)
    if not video.is_file():
        print(f"文件不存在：{video}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    src = video
    if args.seconds > 0 or args.start > 0:
        src = tmp / "cut.mp4"
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if args.start > 0:
            cmd += ["-ss", str(args.start)]
        cmd += ["-i", str(video)]
        if args.seconds > 0:
            cmd += ["-t", str(args.seconds)]
        cmd += ["-c", "copy", str(src)]
        subprocess.run(cmd, check=True)

    media = asr.probe_duration(src) or args.seconds
    print(f"样本：{src}  时长 {media:.1f}s  模型 {args.model}  CPU int8  线程 {threads}"
          f"  批 {args.batch}")

    audio = tmp / "a.wav"
    t0 = time.time()
    asr.extract_audio(src, audio)
    extract_s = time.time() - t0
    print(f"抽音频：{extract_s:.1f}s（{media / extract_s:.1f}×实时）")
    if args.stage == "extract":
        return 0

    t0 = time.time()
    segments, language, duration = asr.transcribe_audio(
        audio, args.model, threads, "ja",
        beam_size=args.beam, batch_size=args.batch, total_duration=media,
    )
    asr_s = time.time() - t0
    print(f"转写：{asr_s:.1f}s  语言={language}  片段={len(segments)}  {media / asr_s:.2f}×实时")

    if segments:
        stats = describe([Cue(s["start"], s["end"], s["text"]) for s in segments])
        print(f"时间轴：跨度 {stats['span_s']:.1f}s  占用率 {stats['coverage']:.3f}  "
              f"单条中位 {stats['median_cue_s']:.2f}s  P95 {stats['p95_cue_s']:.2f}s  "
              f"最长 {stats['max_cue_s']:.2f}s  >8s {stats['over_8s']} 条  "
              f"大空档 {stats['gaps_gt_2s']} 处")
        if args.srt:
            frames = asr.transcript_to_cues(segments)
            cues = format_cues([SrtCue(i + 1, f["start"], f["end"], f["text"])
                                for i, f in enumerate(frames)])
            Path(args.srt).write_text(render_srt(cues), encoding="utf-8")
            print(f"字幕已写出：{args.srt}")
            print("  对比参考字幕：python tools/subtitle_compare.py 参考.srt " + args.srt)

    if args.stage == "full":
        total = extract_s + asr_s
        print(f"合计：{total:.1f}s  {media / total:.2f}×实时")

    rate = media / asr_s
    for hours in (100, 500, 1000, 5000):
        print(f"  {hours:>5} 小时素材 → {hours / rate:.1f} 小时")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
