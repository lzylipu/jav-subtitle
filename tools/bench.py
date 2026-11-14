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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--model", default=os.getenv("WHISPER_MODEL", "small"))
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=0, help="只取前 N 秒，0 表示整片")
    ap.add_argument("--stage", default="asr", choices=["asr", "extract", "full"],
                    help="asr=只要转写；extract=只要抽音频；full=抽音频+转写")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.is_file():
        print(f"文件不存在：{video}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    src = video
    if args.seconds > 0:
        src = tmp / "cut.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-t", str(args.seconds),
                        "-c", "copy", str(src)], check=True)

    media = asr.probe_duration(src) or args.seconds
    print(f"样本：{src}  时长 {media:.1f}s  模型 {args.model}  线程 {args.threads}  批 {args.batch}")

    audio = tmp / "a.wav"
    t0 = time.time()
    asr.extract_audio(src, audio)
    extract_s = time.time() - t0
    print(f"抽音频：{extract_s:.1f}s（{media / extract_s:.1f}×实时）")
    if args.stage == "extract":
        return 0

    t0 = time.time()
    segments, language, duration = asr.transcribe_audio(
        audio, args.model, args.compute, args.threads, "ja",
        beam_size=args.beam, batch_size=args.batch, total_duration=media,
    )
    asr_s = time.time() - t0
    print(f"转写：{asr_s:.1f}s  语言={language}  片段={len(segments)}  {media / asr_s:.2f}×实时")

    if args.stage == "full":
        total = extract_s + asr_s
        print(f"合计：{total:.1f}s  {media / total:.2f}×实时")

    rate = media / asr_s
    for hours in (100, 500, 1000, 5000):
        print(f"  {hours:>5} 小时素材 → {hours / rate:.1f} 小时")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
