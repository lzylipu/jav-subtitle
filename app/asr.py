"""CPU audio extraction and Whisper transcription."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# faster_whisper 只在真正要跑模型时才导入。
# 这样 app.asr 里的纯函数（重切句、trim、去重复）可以被单独测试，
# 不必先装几百 MB 的模型依赖；CI 和本地跑单测都快很多。

# ASR 推理串行化：同一个 2 核/3GB 主机上并发加载两份模型会直接 OOM。
# 翻译属于网络等待，可以并发；ASR 的模型和工作区是内存瓶颈，只能串行。
_ASR_INFERENCE_LOCK = threading.RLock()

# 只保留当前模型。旧实例不能在设置切换时与新实例同时驻留内存。
_MODEL_CACHE: dict[tuple[str, int], tuple[Any, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _load_engine() -> tuple[type, Any]:
    """延迟导入 faster_whisper。返回 (WhisperModel, BatchedInferencePipeline)。"""
    from faster_whisper import WhisperModel

    try:  # faster-whisper >= 1.1
        from faster_whisper import BatchedInferencePipeline
    except ImportError:  # pragma: no cover
        BatchedInferencePipeline = None  # type: ignore[assignment]
    return WhisperModel, BatchedInferencePipeline

# 默认线程数上限。再多收益递减，还会跟翻译/抽音频抢内存带宽。
MAX_AUTO_THREADS = 4

# ---------------------------------------------------------------- 推理窗口
#
# 实测（测试机 10.10.0.8，medium/int8/2 线程）：
#   整片一次推理  → 66 段横跨 3520 秒却只吐 605 个词（大量「一句话拖几分钟」）
#   600 秒窗口化  → 词数 +21%，字幕条目 +35%
# 原因：faster-whisper 的 decoder 注意力窗口有限，长上下文下会跳过弱语音、
# 把静音段硬连成一条。切片推理把它压回模型擅长的长度。
ASR_WINDOW_SECONDS = 600.0
# 窗口之间留一点重叠，避免恰好切在词中间造成首尾漏字。
ASR_WINDOW_OVERLAP_SECONDS = 2.0

# ---------------------------------------------------------------- VAD 参数
#
# Silero VAD 默认 threshold=0.5 / min_silence_duration_ms=2000，对这类片子偏严：
# 实测同一段 300 秒音频，default 只认出 57 个词，低阈值认出 96 个（+68%）。
# 但阈值再低会把音乐/环境声当语音，所以这里用「默认 → 宽松」两级，
# 先跑默认，全片语音覆盖率过低时再跑一遍宽松的。
#
# 全片语音覆盖率低于这个值（对白时长 / 片长），判定「漏了一大段」，触发重跑。
# 真人日文字幕的覆盖率实测在 0.35~0.80；低于 0.12 基本可以确定漏了。
VAD_COVERAGE_FLOOR = 0.12

# VAD 降级阶梯：从严到松，一级比一级更敢把低音量人声算作语音。
# 最后一级（0.05）只在前两级都救不回来时用——阈值再低就会把 BGM 当人声，
# 宁可多一点噪声输入，也不要整段人声缺失。
VAD_LADDER = (
    dict(max_speech_duration_s=20.0, min_silence_duration_ms=500,
         speech_pad_ms=200, threshold=0.5),
    dict(max_speech_duration_s=20.0, min_silence_duration_ms=2000,
         speech_pad_ms=400, threshold=0.2),
    dict(max_speech_duration_s=20.0, min_silence_duration_ms=2000,
         speech_pad_ms=500, threshold=0.05),
)
VAD_STRICT = dict(VAD_LADDER[0])
VAD_RELAXED = dict(VAD_LADDER[1])

# 最后一档：完全关掉 VAD，把整条音轨交给模型。
# 这是「VAD 把人声判成噪声」的终极兜底——实测同一段素材，
# 关 VAD 后识别的词数比 threshold=0.05 还多（187 vs 158）。
# 代价是可能多出幻觉，所以只在前面所有档位都救不回来时才启用。
VAD_OFF = {"vad_filter": False}

# 抽出来的音轨平均音量低于这个值（dBFS），说明片源本身压得很轻，
# Silero VAD 会把大段对白判成非语音。实测 IPZZ-763 平均 -29.4dB，
# 不做增益补偿时整片 116 分钟只认出 94 条字幕（占用率 2.1%）。
AUDIO_TARGET_MEAN_DB = -20.0
AUDIO_MIN_MEAN_DB = -26.0
AUDIO_MAX_GAIN_DB = 24.0

# 词级置信度低于这个值的多半是幻觉（模型在音乐/喘息上编的字）。
# 只砍最底下的：实测真实日语首词也常只有 0.10~0.18（サ/あ/こ 这类），
# 阈值抬到 0.15 会误删正常对白；0.0 的胡话才是确定要丢的。
WORD_PROB_FLOOR = 0.05


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
    tmp = output.with_name(output.name + ".part")
    tmp.unlink(missing_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-f", "wav", str(tmp)],
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
        tmp.replace(output)
        return
    # 失败时删除半成品，避免下次重试把损坏音频当成有效缓存。
    tmp.unlink(missing_ok=True)
    # ffmpeg 的原始报错（一长串命令行 + returncode 183）对用户毫无意义，
    # 真正的原因几乎都是「文件损坏 / 不是视频 / 没有音轨」，直接说清楚。
    detail = (proc.stderr or "").strip().splitlines()
    tail = detail[-1][:200] if detail else ""
    if not video.is_file():
        raise ValueError(f"视频文件不存在：{video}")
    if "does not contain any stream" in (proc.stderr or "") or "no stream" in (proc.stderr or ""):
        raise ValueError(f"视频里没有音轨，无法转写：{video.name}")
    raise ValueError(
        f"视频解码失败，文件可能损坏或不是视频：{video.name}"
        + (f"（{tail}）" if tail else ""))


def audio_mean_db(audio: Path) -> float:
    """测一遍音轨的平均音量（dBFS）。取不到就返回 0（表示「不需要处理」）。

    只用 ffmpeg 自带的 volumedetect，不引入 numpy；这是抽完音频后的一次
    快速探测，成本可以忽略。
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(audio),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
    except Exception:
        return 0.0
    for line in (proc.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def normalize_audio(audio: Path, target_mean_db: float = AUDIO_TARGET_MEAN_DB,
                    min_mean_db: float = AUDIO_MIN_MEAN_DB,
                    max_gain_db: float = AUDIO_MAX_GAIN_DB) -> tuple[float, bool]:
    """音轨太轻就做定增益，把平均音量拉到 target_mean_db。

    返回 (增益 dB, 是否真的改了文件)。这是「漏识别」的真凶之一：
    有些片源整体压得很轻（实测 IPZZ-763 平均 -29.4dB），Silero VAD 的
    能量判据会把大段对白判成非语音，Whisper 只能认出零星几句。
    定增益比压缩器稳：不会像动态压缩那样把 BGM 一起顶起来。

    只有平均音量低于 min_mean_db 才处理，-30dB 以上属于正常片源，
    强行提升只会放大底噪、让 VAD 更乱。
    """
    mean = audio_mean_db(audio)
    # 0 表示「探测失败」。负值是正常的 dBFS，不能当成失败。
    if mean == 0.0 or mean > min_mean_db:
        return 0.0, False
    gain = max(0.0, min(max_gain_db, target_mean_db - mean))
    if gain <= 0.5:
        return 0.0, False
    tmp = audio.with_suffix(".norm.wav")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(audio), "-af", f"volume={gain:.2f}dB",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"音频增益失败：{(proc.stderr or '').strip()[-160:]}")
    tmp.replace(audio)
    return gain, True


def get_model(name: str, cpu_threads: int):
    """按 (模型, 线程数) 缓存唯一模型实例。

    固定 device="cpu" + compute_type="int8"：
    CTranslate2 只支持 CPU / NVIDIA CUDA / AMD ROCm 三种后端，Intel 核显、AMD 核显
    都不在里面，而为了用显卡换一套引擎并不值当（详见 README）。所以这条路只走 CPU。
    """
    key = (name, cpu_threads)
    with _CACHE_LOCK:
        if key not in _MODEL_CACHE:
            whisper_model_cls, batched_cls = _load_engine()
            model = whisper_model_cls(name, device="cpu", compute_type="int8",
                                     cpu_threads=cpu_threads)
            batched = batched_cls(model=model) if batched_cls else None
            _MODEL_CACHE[key] = (model, batched)
        return _MODEL_CACHE[key]


def clear_cache() -> None:
    """改模型/线程数后调用，丢掉旧实例。

    必须和推理共用同一把锁：否则设置切换可能在新模型加载时把旧模型清掉，
    两个实例短时间同时驻留内存。
    """
    with _ASR_INFERENCE_LOCK:
        with _CACHE_LOCK:
            _MODEL_CACHE.clear()


def memory_available_bytes() -> int:
    """当前还能用多少内存（字节）。取不到就返回一个保守的小值。

    3GB 的小机器上，medium 模型峰值 2.4GB 会被 cgroup OOM 杀掉（实测 dmesg
    `anon-rss:2408484kB`），所以转写前必须看一眼余量。
    """
    try:  # cgroup v2
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw.isdigit():
            current = Path("/sys/fs/cgroup/memory.current").read_text().strip()
            if current.isdigit():
                return max(0, int(raw) - int(current))
    except Exception:
        pass
    try:  # cgroup v1
        raw = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text().strip()
        usage = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes").read_text().strip()
        limit = int(raw)
        # v1 里「没有限制」是个接近天文数字的值
        if limit < (1 << 60):
            return max(0, limit - int(usage))
    except Exception:
        pass
    try:
        info = Path("/proc/meminfo").read_text()
        for line in info.splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


# 各模型在 int8 下大约需要的常驻内存（含 ctranslate2 工作区），用于选型护栏。
# 这是「模型权重 + 推理工作区」的预算，不是进程 RSS 的精确上限；实测 medium
# 在 3GB 机器上的峰值 RSS 约 2.8GB，因此阈值必须留出系统、音频和容器余量。
MODEL_MEMORY_HINT = {
    "tiny": 0.6,
    "base": 0.8,
    "small": 1.1,
    "medium": 2.0,
    "large": 3.2,
    "large-v2": 3.2,
    "large-v3": 3.4,
}

# 小内存机器必须给系统、ffmpeg、SQLite 和容器运行时留出余量。只拿可用内存
# 的 70% 做模型预算，仍不足以解释 3GB 主机上 medium 接近 2.8GB 的峰值 RSS。
MODEL_MEMORY_RESERVE_BYTES = 768 * (1 << 20)


def recommend_model(requested: str, available_bytes: int | None = None) -> tuple[str, str]:
    """按可用内存给出建议模型。

    返回 (模型, 说明)。内存不够时不是直接失败，而是降档并说明原因 ——
    在 3GB 机器上跑 medium 只会被 OOM 杀掉，等于白跑。

    预算按「当前可用内存 - 固定系统余量」计算，避免只看机器总内存或瞬时
    MemAvailable 时，把 medium 放进一台实际上已经放不下的 3GB LXC。
    """
    avail = memory_available_bytes() if available_bytes is None else available_bytes
    budget = max(0, avail - MODEL_MEMORY_RESERVE_BYTES)
    need = MODEL_MEMORY_HINT.get(requested, 1.5) * (1 << 30)
    if avail and need > budget:
        # 从大到小找一个装得下的
        for name in ("large-v3", "large-v2", "large", "medium", "small", "base", "tiny"):
            if MODEL_MEMORY_HINT[name] * (1 << 30) <= budget:
                return name, (f"可用内存 {avail / (1 << 30):.1f}GB 装不下 {requested}"
                              f"（需约 {need / (1 << 30):.1f}GB，留出系统余量后仅"
                              f" {budget / (1 << 30):.1f}GB），已降级为 {name}")
        return "tiny", (f"可用内存 {avail / (1 << 30):.1f}GB 非常紧张，"
                        f"已降级为 tiny，请保持机器空闲")
    return requested, ""


def _run_engine(model: Any, batched: Any, path: Path, kwargs: dict[str, Any],
                batch_size: int):
    """按配置调用整轨或批推理引擎。

    推理全程串行：并发 ASR worker 会同时持有两份模型，3GB 主机上直接 OOM。
    """
    with _ASR_INFERENCE_LOCK:
        if batched is not None and batch_size > 1:
            return batched.transcribe(str(path), batch_size=batch_size, **kwargs)
        return model.transcribe(str(path), **kwargs)


def _collect_words(segments: Iterable[Any], offset: float = 0.0) -> list[dict[str, Any]]:
    """把带词级时间戳的 segment 摊平成词表，并补上窗口偏移。"""
    words: list[dict[str, Any]] = []
    for segment in segments:
        for word in (getattr(segment, "words", None) or []):
            token = (word.word or "").strip()
            if not token:
                continue
            words.append({
                "start": float(word.start) + offset,
                "end": float(word.end) + offset,
                "text": token,
                "prob": float(getattr(word, "probability", 1.0)),
            })
    return words


def merge_words(target: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> None:
    """把相邻窗口的词并入总表，去掉重叠区里被重复识别出来的同一个词。

    只认「文本相同 + 起点相差不到 0.6 秒」为重复，不做模糊匹配：
    短助词（は / が / を）本来就密集，模糊合并会把正常对白吃掉。

    候选不能只查最后 8 个词：2 秒重叠区可能包含十几个密集日语词，
    固定窗口会漏合并并制造重复字幕。这里按时间范围扫描所有可能重叠的词。
    """
    for word in incoming:
        duplicate = None
        for prev in target:
            if prev["start"] > word["start"] + 0.6:
                break
            if (prev["text"] == word["text"]
                    and abs(prev["start"] - word["start"]) < 0.6):
                duplicate = prev
                break
        if duplicate is None:
            target.append(word)
        else:
            duplicate["end"] = max(duplicate["end"], word["end"])


def window_ranges(duration: float, window: float = ASR_WINDOW_SECONDS,
                  overlap: float = ASR_WINDOW_OVERLAP_SECONDS) -> list[tuple[float, float]]:
    """把整轨切成有重叠的窗口，返回 [(start, 时长)]。"""
    if duration <= 0:
        return [(0.0, 0.0)]
    if duration <= window:
        return [(0.0, duration)]
    step = max(1.0, window - overlap)
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(duration, start + window)
        ranges.append((start, end - start))
        if end >= duration:
            break
        start += step
    return ranges


def _slice_audio(src: Path, dst: Path, start: float, duration: float) -> None:
    """用 ffmpeg 切一段音频出来。转成 wav 是为了让模型只解一次码。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        raise ValueError(f"音频切片失败（{start:.0f}s）：{tail[-1][:160] if tail else 'ffmpeg 返回非零'}")


def speech_seconds(words: list[dict[str, Any]]) -> float:
    """所有发声区间的并集长度（词之间不重复计时）。"""
    spans = sorted((float(w["start"]), float(w["end"])) for w in words
                   if float(w["end"]) > float(w["start"]))
    total = 0.0
    current_start = current_end = None
    for start, end in spans:
        if current_end is None:
            current_start, current_end = start, end
        elif start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    if current_end is not None:
        total += current_end - current_start
    return total


def speech_coverage(words: list[dict[str, Any]], duration: float) -> float:
    """语音占全片时长的比例。用来判断 VAD 是不是漏了一大段。"""
    if duration <= 0:
        return 1.0
    return min(1.0, speech_seconds(words) / duration)


def filter_low_confidence(words: list[dict[str, Any]],
                          floor: float = WORD_PROB_FLOOR) -> list[dict[str, Any]]:
    """丢掉置信度低到确定是幻觉的词（实测编出来的字概率是 0.0）。"""
    return [w for w in words if float(w.get("prob", 1.0)) >= floor
            and float(w["end"]) > float(w["start"])]


def _collect_windowed(model: Any, batched: Any, audio: Path, duration: float,
                      ranges: list[tuple[float, float]], kwargs: dict[str, Any],
                      batch_size: int, progress: Callable[[float, str], None] | None
                      ) -> tuple[list[dict[str, Any]], str]:
    """逐窗推理，把词级时间戳拼回整条时间轴。"""
    words: list[dict[str, Any]] = []
    language = ""
    tmpdir = Path(tempfile.mkdtemp(prefix="asr-window-"))
    try:
        for index, (start, length) in enumerate(ranges):
            whole = start <= 0 and length >= duration - 0.01
            path = audio
            if not whole:
                path = tmpdir / f"{index}.wav"
                _slice_audio(audio, path, start, length)
            segments, info = _run_engine(model, batched, path, kwargs, batch_size)
            language = language or getattr(info, "language", "") or ""
            incoming = _collect_words(segments, 0.0 if whole else start)
            merge_words(words, incoming)
            if progress:
                progress(min((start + length) / duration, 1.0),
                         incoming[-1]["text"] if incoming else "")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return words, language


def _vad_ladder(model: Any, batched: Any, audio: Path, duration: float,
                ranges: list[tuple[float, float]], base: dict[str, Any],
                batch_size: int, progress: Callable[[float, str], None] | None,
                initial_words: list[dict[str, Any]] | None = None,
                initial_language: str = "",
                initial_threshold: float | None = None
                ) -> tuple[list[dict[str, Any]], str]:
    """按 VAD 阶梯逐级尝试，返回覆盖率最高的一档结果。

    严格档（threshold 0.5）先跑；覆盖率低于 VAD_COVERAGE_FLOOR 就退一档，
    最多退到 threshold 0.05；仍然不达标就彻底关掉 VAD，把整轨交给模型。
    每档都比上一档更宽松，所以只要有一档把人声捞回来就停，
    不会白白跑完全部档位。

    ``initial_words`` 用于复用已经跑过的严格档结果，避免短音频把 threshold=0.5
    白跑两遍。进度回调按阶梯映射到单调区间，重试时界面不会倒退。
    """
    best_words: list[dict[str, Any]] = list(initial_words or [])
    best_coverage = speech_coverage(best_words, duration) if best_words else -1.0
    language = initial_language
    plans: list[dict[str, Any]] = [{**base, "vad_parameters": dict(vad)}
                                   for vad in VAD_LADDER]
    plans.append({**base, **VAD_OFF})
    start_level = 0
    if initial_threshold is not None:
        for index, kwargs in enumerate(plans):
            threshold = kwargs.get("vad_parameters", {}).get("threshold")
            if threshold == initial_threshold:
                start_level = index
                break
    for level, kwargs in enumerate(plans[start_level:], start=start_level):
        if level == start_level and initial_words is not None:
            words, lang = list(initial_words), initial_language
        else:
            words, lang = _collect_windowed(
                model, batched, audio, duration, ranges, kwargs, batch_size, progress)
        coverage = speech_coverage(words, duration)
        language = language or lang
        if coverage > best_coverage:
            best_words, best_coverage = words, coverage
        if coverage >= VAD_COVERAGE_FLOOR:
            break
        label = ("关闭 VAD" if level == len(plans) - 1
                 else f"threshold {kwargs['vad_parameters']['threshold']:.2f}")
        logger.info("VAD 第 %d 档（%s）覆盖率 %.1f%%，继续放宽",
                    level + 1, label, coverage * 100)
    return best_words, language


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

    ## 为什么要切窗口

    整片一次推理时，模型在长上下文下会跳过弱语音、把静音硬连成一条。实测同一部片
    600 秒窗口化后词数 +21%、字幕条目 +35%。窗口之间留 2 秒重叠，重叠区的重复词
    由 `merge_words` 去掉。

    ## VAD 为什么要两级

    默认阈值对轻声道白偏严。先跑严格档，若全片语音覆盖率低于 12%，再用宽松档重跑，
    取覆盖率更高的一份；只在真正像漏了大段时才付第二遍的代价。
    """
    model, batched = get_model(model_name, cpu_threads)
    base: dict[str, Any] = dict(language=language or None, vad_filter=True,
                                beam_size=beam_size, condition_on_previous_text=False)
    use_words = bool(word_timestamps) and not (batch_size > 1)
    if use_words:
        base["word_timestamps"] = True

    duration = probe_duration(audio) or float(total_duration or 0)
    language_out = ""

    # 片源整体太轻时，先做定增益再送进模型。不做这一步，VAD 会把大段
    # 对白判成非语音（实测 IPZZ-763 整片只认出 94 条）。
    # 增益后的副本单独放临时目录，不改原始音轨文件。
    work_audio = audio
    tmpdir: Path | None = None
    if audio.is_file():
        mean = audio_mean_db(audio)
        # -80dB 以下基本是静音轨，增益也没用，别浪费一遍 ffmpeg。
        if mean < AUDIO_MIN_MEAN_DB and mean > -80.0:
            gain = max(0.0, min(AUDIO_MAX_GAIN_DB, AUDIO_TARGET_MEAN_DB - mean))
            if gain > 0.5:
                tmpdir = Path(tempfile.mkdtemp(prefix="asr-gain-"))
                boosted = tmpdir / "boosted.wav"
                proc = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-i", str(audio), "-af", f"volume={gain:.2f}dB",
                     "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(boosted)],
                    capture_output=True, text=True, timeout=1800,
                )
                if proc.returncode == 0:
                    logger.info("音轨平均音量 %.1fdB，增益 %.1fdB 后转写：%s",
                                mean, gain, audio.name)
                    work_audio = boosted
                else:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    tmpdir = None

    try:
        if use_words:
            if duration > ASR_WINDOW_SECONDS and work_audio.is_file():
                ranges = window_ranges(duration)
                words, language_out = _vad_ladder(
                    model, batched, work_audio, duration, ranges, base,
                    batch_size, progress)
            else:
                segments, info = _run_engine(model, batched, work_audio, {**base,
                                         "vad_parameters": dict(VAD_STRICT)}, batch_size)
                language_out = getattr(info, "language", "") or ""
                duration = float(getattr(info, "duration", 0) or duration or 0)
                words = _collect_words(segments)
                if (duration >= 60 and speech_coverage(words, duration) < VAD_COVERAGE_FLOOR
                        and work_audio.is_file() and work_audio.stat().st_size > 0):
                    words, language_out = _vad_ladder(
                        model, batched, work_audio, duration,
                        [(0.0, duration)], base, batch_size, progress,
                        initial_words=words, initial_language=language_out,
                        initial_threshold=VAD_STRICT["threshold"])
            words = filter_low_confidence(words)
            result = resegment(words, gap=split_gap,
                               max_seconds=max_cue_seconds, max_chars=max_cue_chars)
        else:
            segments, info = _run_engine(model, batched, work_audio, base, batch_size)
            language_out = getattr(info, "language", "") or ""
            duration = float(getattr(info, "duration", 0) or duration or 0)
            result = []
            for segment in segments:
                text = segment.text.strip()
                if text:
                    result.append({"start": float(segment.start), "end": float(segment.end),
                                   "text": text})
                if progress and duration:
                    progress(min(float(segment.end) / duration, 1.0), text)
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # 重切之后再收一遍尾巴：模型会把短句的时间拉得很长，字幕就挂在那儿没人说话
    result = drop_repeats(trim_cues(result))
    return result, language_out, duration


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


def trim_cues(segments: list[dict[str, Any]], max_cps: float = 2.5,
              slack: float = 1.2, min_seconds: float = 1.5) -> list[dict[str, Any]]:
    """把「文字很少、时间却拉得很长」的字幕条收回来。

    whisper 在没人说话的地方（喘息、音乐、环境声）会硬凑出一句，
    还把这句的起止拉成几十秒，屏幕上就是「字幕挂十几秒，可那时候根本没人说话」。

    判据用「字数 ÷ 语速 + 余量」：日语正常语速 5 字/秒以上，这里按 2.5 字/秒算
    （很慢地说话也够用），再给 1.2 秒余量、下限 1.5 秒 —— 正常的一句话不会被削，
    只有那种「两个字占 85 秒」的假条会被砍回合理长度。
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg["start"])
        end = float(seg["end"])
        allowed = max(min_seconds, len(text) / max_cps + slack)
        if end - start > allowed:
            end = start + allowed
        if end <= start:
            continue
        out.append({"start": start, "end": end, "text": text})
    return out


def drop_repeats(segments: list[dict[str, Any]], window: float = 2.0) -> list[dict[str, Any]]:
    """去掉紧挨着、内容一模一样的条（模型在喘气声上反复吐同一句）。"""
    out: list[dict[str, Any]] = []
    for seg in segments:
        if out and seg["text"] == out[-1]["text"] and seg["start"] - out[-1]["end"] <= window:
            out[-1]["end"] = max(out[-1]["end"], seg["end"])
            continue
        out.append(dict(seg))
    return out


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
