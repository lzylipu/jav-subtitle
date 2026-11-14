"""CPU audio extraction and Whisper transcription."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from faster_whisper import WhisperModel

try:  # faster-whisper >= 1.1
    from faster_whisper import BatchedInferencePipeline
except ImportError:  # pragma: no cover
    BatchedInferencePipeline = None  # type: ignore[assignment]

# 每个线程一份模型实例：ctranslate2 内部有线程池，跨线程共享容易互相拖慢
_CACHE: dict[tuple[int, str, int], tuple[WhisperModel, Any]] = {}
_CACHE_LOCK = threading.Lock()

# 默认线程数上限。再多收益递减，还会跟翻译/抽音频抢内存带宽。
MAX_AUTO_THREADS = 4


def threads_for(cpu_threads: int) -> int:
    """设置里 0 表示自动：取 min(4, 核数)。"""
    if cpu_threads and cpu_threads > 0:
        return int(cpu_threads)
    return max(1, min(MAX_AUTO_THREADS, os.cpu_count() or 1))


def probe_duration(video: Path) -> float:
    """用 ffprobe 读时长，几毫秒的事，比事后猜准确。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def extract_audio(video: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(output)],
        check=True, capture_output=True, text=True, timeout=3600,
    )


def get_model(name: str, cpu_threads: int):
    """按 (模型, 线程数) 缓存模型；改设置就换一份，不混用。

    固定 device="cpu" + compute_type="int8"：
    CTranslate2 只支持 CPU / NVIDIA CUDA / AMD ROCm 三种后端，Intel 核显、AMD 核显
    都不在里面，而为了用显卡换一套引擎并不值当（详见 README）。所以这条路只走 CPU。
    """
    key = (threading.get_ident(), name, cpu_threads)
    with _CACHE_LOCK:
        if key not in _CACHE:
            model = WhisperModel(name, device="cpu", compute_type="int8",
                                 cpu_threads=cpu_threads)
            batched = BatchedInferencePipeline(model=model) if BatchedInferencePipeline else None
            _CACHE[key] = (model, batched)
        return _CACHE[key]


def clear_cache() -> None:
    """改模型/线程数后调用，丢掉旧实例。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def transcribe_audio(
    audio: Path,
    model_name: str,
    cpu_threads: int,
    language: str,
    beam_size: int = 1,
    batch_size: int = 8,
    progress: Callable[[float, str], None] | None = None,
    total_duration: float = 0.0,
) -> tuple[list[dict[str, Any]], str, float]:
    model, batched = get_model(model_name, cpu_threads)
    kwargs: dict[str, Any] = dict(language=language or None, vad_filter=True, beam_size=beam_size)

    if batched is not None and batch_size > 1:
        segments, info = batched.transcribe(str(audio), batch_size=batch_size, **kwargs)
    else:
        segments, info = model.transcribe(str(audio), **kwargs)

    duration = float(getattr(info, "duration", 0) or total_duration or 0)
    result: list[dict[str, Any]] = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            result.append({"start": float(segment.start), "end": float(segment.end), "text": text})
        if progress and duration:
            progress(min(float(segment.end) / duration, 1.0), text)
    return result, getattr(info, "language", "") or "", duration


def seconds_to_timestamp(value: float) -> str:
    value = max(0.0, value)
    ms = round(value * 1000)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def transcript_to_cues(segments: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"start": seconds_to_timestamp(s["start"]), "end": seconds_to_timestamp(s["end"]), "text": s["text"]}
        for s in segments
    ]
