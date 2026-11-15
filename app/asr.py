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
    word_timestamps: bool = True,
    split_gap: float = 0.7,
    max_cue_seconds: float = 8.0,
    max_cue_chars: int = 42,
) -> tuple[list[dict[str, Any]], str, float]:
    """转写整轨音频，返回 (字幕段, 语言, 时长)。

    ## 为什么一定要开 word_timestamps

    只开 `vad_filter=True` 时，faster-whisper 会把 VAD 判定的连续语音块整块交给模型，
    模型自己决定在哪断句；中间夹着音乐、环境声或停顿稍长的对白会被糊成一条。
    实测 15 句真值对白被压成 **4 条**，单条横跨 60～200 秒，字幕和画面对不上。

    开词级时间戳后，边界由对齐模型的逐词位置决定，再按「词间静音 > split_gap」
    重新切句，起点偏差从 41 秒降到 0.26 秒。代价是慢：40x 实时 → 1.9x 实时。

    这是字幕项目，时间轴错了等于白干，所以默认开。
    `condition_on_previous_text=False`：不让上一句的文本当下一句的 prompt，
    否则模型会把前一句的词续写到后一句开头（实测出现过这种串话）。
    """
    model, batched = get_model(model_name, cpu_threads)
    kwargs: dict[str, Any] = dict(language=language or None, vad_filter=True, beam_size=beam_size,
                                  condition_on_previous_text=False)

    use_words = bool(word_timestamps) and batched is None
    if use_words:
        kwargs["word_timestamps"] = True

    if batched is not None and batch_size > 1:
        segments, info = batched.transcribe(str(audio), batch_size=batch_size, **kwargs)
    else:
        segments, info = model.transcribe(str(audio), **kwargs)

    duration = float(getattr(info, "duration", 0) or total_duration or 0)
    result: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []

    for segment in segments:
        text = segment.text.strip()
        if use_words and getattr(segment, "words", None):
            for w in segment.words:
                token = (w.word or "").strip()
                if not token:
                    continue
                words.append({"start": float(w.start), "end": float(w.end), "text": token,
                              "prob": float(getattr(w, "probability", 1.0))})
        elif text:
            result.append({"start": float(segment.start), "end": float(segment.end), "text": text})
        if progress and duration:
            progress(min(float(segment.end) / duration, 1.0), text)

    if use_words and words:
        result = resegment(words, gap=split_gap,
                           max_seconds=max_cue_seconds, max_chars=max_cue_chars)
    return result, getattr(info, "language", "") or "", duration


# 句末标点：超过长度上限时优先在这些字符后面断
SENTENCE_END = "。！？!?…"


def resegment(words: list[dict[str, Any]], gap: float = 0.7,
              max_seconds: float = 8.0, max_chars: int = 42) -> list[dict[str, Any]]:
    """把词流按「静音间隔 / 时长 / 字数」重新切成字幕条。

    - 词间间隔超过 ``gap`` 就断（这是最主要的分句依据，对应画面上真的停下来了）；
    - 单条超过 ``max_seconds`` 秒也断，免得一条字幕挂十几秒；
    - 单条超过 ``max_chars`` 字时，在最近的句末标点处断，没有标点就硬断。

    时间轴用「首词的 start / 末词的 end」，不做额外外扩 —— 字幕比声音早出现或晚消失
    都会被看出来，严格贴着发声区间最稳。
    """
    if not words:
        return []

    def flush(group: list[dict[str, Any]], out: list[dict[str, Any]]) -> None:
        if not group:
            return
        text = "".join(w["text"] for w in group).strip()
        if text:
            out.append({"start": group[0]["start"], "end": group[-1]["end"], "text": text})

    result: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    chars = 0

    for word in words:
        if group:
            separates = word["start"] - group[-1]["end"] > gap
            too_long = word["end"] - group[0]["start"] > max_seconds
            too_wide = chars + len(word["text"]) > max_chars
            if separates or too_long or too_wide:
                # 只是字数超了：优先在组内最后一个句号处切，别把句子从中间砍断
                if not separates and not too_long and not _ends_sentence(group):
                    head, tail = _split_at_punctuation(group)
                    if head:
                        flush(head, result)
                        group, chars = tail, sum(len(w["text"]) for w in tail)
                        group.append(word)
                        chars += len(word["text"])
                        continue
                flush(group, result)
                group, chars = [], 0
        group.append(word)
        chars += len(word["text"])
    flush(group, result)
    return result


def _ends_sentence(group: list[dict[str, Any]]) -> bool:
    return bool(group) and group[-1]["text"][-1:] in SENTENCE_END


def _split_at_punctuation(group: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """在组内最后一个句末标点之后切开，返回 (该收尾的部分, 要带到下一组的部分)。

    找不到标点就返回 (全部, []) —— 调用方会按「硬断」处理。
    """
    for idx in range(len(group) - 1, 0, -1):
        if group[idx - 1]["text"][-1:] in SENTENCE_END:
            return group[:idx], group[idx:]
    return group, []


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
