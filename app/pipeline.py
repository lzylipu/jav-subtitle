"""Stage-separated pipeline: extract -> asr -> translate.

每个阶段有自己的待办队列和独立的 worker 池，所以一条片子转写时可以同时有
其它片子在做翻译。单条片子内部仍是顺序执行，避免半截字幕。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import asr, scraping, subtitles
from .config import AUDIO_DIR, DATA_DIR, Settings
from .db import (
    add_event, advance, claim, connect, counter_add, counter_get, fail, finish,
    load_settings, now, stage_label, today, tm_lookup, tm_store, update_job,
)
from .ratelimit import limiter
from .subtitles import Cue, atomic_write, parse_srt, render_srt, target_paths
from .translation import (FatalTranslationError, MissingConfigError, build_translator,
                          cache_namespace, normalize_provider, parse_fallback,
                          translator_cache_key)

log = logging.getLogger(__name__)

WORK_DIR = DATA_DIR / "work"
MAX_POOL = 16                      # 每阶段固定起这么多线程，实际并发由配置决定
POOL_STAGES = ("extract", "asr", "translate")

stop_event = threading.Event()
pause_event = threading.Event()
pause_info: dict[str, str] = {"reason": ""}
budget_block_until: dict[str, float] = {"ts": 0.0}
config_wait_until: dict[str, float] = {"ts": 0.0}
config_wait_reason: dict[str, str] = {"text": ""}
# 最近一次翻译阶段的错误。翻译引擎内部要重试几分钟才冒泡，
# 这段时间任务表只显示进度不动，用户会以为「卡住了」——把它暴露到 /api/stats，
# 前端直接显示「翻译接口最近一次报错：401 ...」
last_translate_error = {"text": "", "ts": 0.0}   # type: dict
_threads: list[threading.Thread] = []
_local = threading.local()
_settings_cache: dict[str, object] = {"ts": 0.0, "value": None}


def current_settings() -> Settings:
    """48 个空闲线程每秒都读配置会把 SQLite 打满，这里缓存 2 秒。"""
    stamp = time.time()
    if _settings_cache["value"] is None or stamp - float(_settings_cache["ts"] or 0) > 2.0:
        _settings_cache["value"] = load_settings()
        _settings_cache["ts"] = stamp
    return _settings_cache["value"]  # type: ignore[return-value]


def invalidate_settings_cache() -> None:
    _settings_cache["value"] = None
    _settings_cache["ts"] = 0.0


class BudgetExceeded(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers


def translator(settings: Settings):
    """按当前渠道造引擎。渠道或任何相关参数变了就重建，否则复用连接。"""
    provider = normalize_provider(settings.translate_provider)
    key = translator_cache_key(provider, settings)
    if getattr(_local, "key", None) != key:
        old = getattr(_local, "tr", None)
        if old is not None:
            old.close()
        _local.key = key
        _local.tr = build_translator(provider, settings, stop_event=stop_event)
    return _local.tr


def _next_midnight() -> float:
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def ensure_budget(need: int, settings: Settings) -> None:
    budget = settings.daily_char_budget
    if budget <= 0:
        return
    used = counter_get(f"chars:{today()}")
    if used + need > budget:
        budget_block_until["ts"] = _next_midnight()
        raise BudgetExceeded(f"今日翻译预算已用完（{int(used)}/{budget} 字），明天 00:00 UTC 后继续")


def pause(reason: str = "手动暂停") -> None:
    pause_info["reason"] = reason
    pause_event.set()


def resume() -> None:
    pause_info["reason"] = ""
    pause_event.clear()


def blocked() -> str:
    if pause_event.is_set():
        return pause_info["reason"] or "已暂停"
    if time.time() < budget_block_until["ts"]:
        return "翻译预算暂停中，等待跨日"
    if time.time() < config_wait_until["ts"]:
        return config_wait_reason["text"] or "翻译接口未配置，等待设置"
    return ""


# --------------------------------------------------------------------- stages


def handle_extract(job: dict, settings: Settings) -> None:
    video = Path(job["video_path"])
    root = Path(settings.media_root).expanduser().resolve()
    if video.is_symlink() or not video.is_file():
        finish(job["id"], "extract", "skipped", "视频已不存在")
        return
    try:
        if not video.resolve().is_relative_to(root):
            finish(job["id"], "extract", "skipped", "视频不在配置的媒体目录内")
            return
    except OSError:
        finish(job["id"], "extract", "skipped", "无法解析视频路径")
        return

    target, japanese = target_paths(video, settings.target_language)
    if target.exists() and not settings.overwrite_existing:
        finish(job["id"], "done", "skipped", "已存在中文字幕", zh_srt=str(target))
        return

    duration = asr.probe_duration(video)
    update_job(job["id"], duration=duration, progress=5,
               message=f"时长 {duration / 60:.1f} 分钟" if duration else "已索引")

    if japanese.is_file():
        advance(job["id"], "translate", 70, "复用已有日文字幕", ja_srt=str(japanese))
        return

    # 先问一遍字幕站：有人做过就不用转写了。
    # 124 分钟的片子转写要二三十分钟纯 CPU 时间，能省则省。
    # 刮削是必经步骤，不再看开关（界面已不给关）。
    if scrape_video(job, settings, video, target, japanese):
        return

    audio = AUDIO_DIR / f"{job['id']}.wav"
    if not audio.exists() or audio.stat().st_size == 0:
        asr.extract_audio(video, audio)
    advance(job["id"], "asr", 10, "音频已提取", audio_path=str(audio))


def scrape_video(job: dict, settings: Settings, video: Path,
                 target: Path, japanese: Path) -> bool:
    """先刮削字幕。返回 True 表示这个任务已经处理完了，调用方别再走转写。

    - 找到中文：直接写成 .zh-CN.srt，任务完事；
    - 找到日文：写成 .ja.srt，推进到翻译阶段（省掉最贵的转写）；
    - 全都找不到：返回 False，老老实实抽音频转写。
    """
    update_job(job["id"], progress=8, message="正在字幕站查找现成字幕")
    try:
        result = scraping.scrape(video.name, settings, job.get("duration") or 0.0)
    except Exception as exc:                      # noqa: BLE001 - 刮削坏了不能拖垮整条流水线
        add_event(job["id"], f"刮削出错，改用转写：{type(exc).__name__}: {exc}", "warn")
        return False

    for line in result.tried:
        add_event(job["id"], f"刮削 {line}", "info" if result.ok else "warn")

    if not result.ok or not result.cues:
        return False

    rendered = subtitles.render_srt(result.cues)
    if result.language == "zh":
        subtitles.atomic_write(target, rendered, overwrite=True)
        finish(job["id"], "done", "done",
               f"刮削到中文字幕（{result.source_name}，{len(result.cues)} 条）",
               zh_srt=str(target), char_count=sum(len(c.text) for c in result.cues))
        add_event(job["id"], f"用 {result.provider} 的中文字幕，跳过转写和翻译", "info")
        return True

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    working = WORK_DIR / f"{job['id']}.ja.srt"
    subtitles.atomic_write(working, rendered, overwrite=True)
    if settings.keep_japanese_srt:
        subtitles.atomic_write(japanese, rendered, overwrite=True)
    advance(job["id"], "translate", 70,
            f"刮削到日文字幕（{result.source_name}，{len(result.cues)} 条），跳过转写",
            ja_srt=str(working), detected_language="ja",
            char_count=sum(len(c.text) for c in result.cues))
    return True


def handle_asr(job: dict, settings: Settings) -> None:
    audio = Path(job["audio_path"] or "")
    if not audio.is_file():
        advance(job["id"], "extract", 0, "音频文件丢失，重新提取")
        return

    started = time.time()
    last_push = [0.0]
    last_progress = [0]

    def progress(ratio: float, text: str) -> None:
        stamp = time.time()
        if stamp - last_push[0] < 2.0 and ratio < 0.99:
            return
        last_push[0] = stamp
        # VAD 阶梯会重新推理；局部窗口进度不能把任务进度从 70% 拉回 10%。
        value = max(last_progress[0], 10 + int(ratio * 60))
        last_progress[0] = value
        update_job(job["id"], progress=value, message=(text or "")[:120])

    # 3GB 小机器上 medium 模型峰值 2.4GB，直接跑会被 cgroup OOM 杀掉。
    # 内存不够时降一档并在任务日志里说明，别让用户只看到「任务莫名失败」。
    model_name, downgrade_note = asr.recommend_model(settings.whisper_model)
    if downgrade_note:
        add_event(job["id"], downgrade_note, "warn")
        update_job(job["id"], message=downgrade_note[:120])

    segments, language, duration = asr.transcribe_audio(
        audio, model_name, asr.threads_for(settings.cpu_threads),
        settings.source_language, beam_size=settings.beam_size,
        batch_size=settings.asr_batch_size, progress=progress,
        total_duration=job["duration"] or 0.0,
        word_timestamps=settings.asr_word_timestamps,
        split_gap=settings.asr_split_gap,
        max_cue_seconds=settings.asr_max_cue_seconds,
        max_cue_chars=settings.asr_max_cue_chars,
    )
    frames = asr.transcript_to_cues(segments)
    if not frames:
        finish(job["id"], "done", "skipped", "未检测到可识别的日语对白")
        if not settings.keep_audio:
            audio.unlink(missing_ok=True)
        return

    cues = [Cue(i, f["start"], f["end"], f["text"]) for i, f in enumerate(frames, 1)]
    rendered = render_srt(cues)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    working = WORK_DIR / f"{job['id']}.ja.srt"
    atomic_write(working, rendered, overwrite=True)
    if settings.keep_japanese_srt:
        atomic_write(target_paths(Path(job["video_path"]), settings.target_language)[1],
                     rendered, overwrite=True)

    elapsed = time.time() - started
    counter_add("asr.media_seconds", duration or 0.0)
    counter_add("asr.wall_seconds", elapsed)
    if not settings.keep_audio:
        audio.unlink(missing_ok=True)

    advance(job["id"], "translate", 70, f"转写完成 {len(cues)} 条（{elapsed:.0f} 秒）",
            ja_srt=str(working), char_count=sum(len(c.text) for c in cues),
            detected_language=language, asr_seconds=elapsed)


def handle_translate(job: dict, settings: Settings) -> None:
    video = Path(job["video_path"])
    target, _ = target_paths(video, settings.target_language)
    if target.exists() and not settings.overwrite_existing:
        finish(job["id"], "done", "skipped", "已存在中文字幕", zh_srt=str(target))
        return

    source = Path(job["ja_srt"] or WORK_DIR / f"{job['id']}.ja.srt")
    if not source.is_file():
        fallback = target_paths(video, settings.target_language)[1]
        source = fallback if fallback.is_file() else source
    if not source.is_file():
        advance(job["id"], "asr", 10, "缺少日文字幕，重新转写")
        return

    cues = parse_srt(source.read_text(encoding="utf-8-sig"))
    if not cues:
        finish(job["id"], "done", "skipped", "日文字幕为空")
        return

    # 翻译记忆按渠道分命名空间：机翻结果不能污染大模型缓存。
    # 走降级链时命名空间跟着「实际干活的渠道」走（每批重新算），
    # 免得主渠道恢复后把备用渠道的机翻结果当成自己的缓存直接返回。
    batch_size = max(1, settings.translation_batch_size)
    engine = translator(settings)
    started = time.time()
    hits = 0
    cached_chars = 0
    translated: list[str] = []
    total = len(cues)
    kept = 0  # 翻不过去、保留日文原文的条数

    for start in range(0, total, batch_size):
        chunk = cues[start:start + batch_size]
        texts = [c.text for c in chunk]
        # 命名空间跟着当前实际干活的渠道走（走降级链时可能是备用渠道）
        provider_now = getattr(engine, "active_name", None) or settings.translate_provider
        model = cache_namespace(provider_now, settings.openai_model)
        cached = tm_lookup(texts, model) if settings.tm_enabled else {}
        result: list[str | None] = [None] * len(chunk)
        missing: list[int] = []
        for i, text in enumerate(texts):
            value = cached.get(f"{model}\x00{text}")
            if value:
                result[i] = value
                hits += 1
                cached_chars += len(text)
            else:
                missing.append(i)

        if missing:
            need = sum(len(texts[i]) for i in missing)
            ensure_budget(need, settings)
            # 翻译引擎内部会重试、拆批、逐条抢救，一卡就是几分钟。
            # 这期间任务表只有「翻译 X/Y 条」不动，用户看不出在干什么，
            # 所以先写一条「正在发第 N 批」的日志，失败时再把原因写进去。
            try:
                outputs = engine.translate([chunk[i] for i in missing])
            except Exception as exc:
                add_event(job["id"],
                          f"翻译第 {start // batch_size + 1} 批（{start + 1}–"
                          f"{start + len(chunk)} 条）失败：{type(exc).__name__}: {exc}",
                          "error")
                raise
            counter_add(f"chars:{today()}", need)
            last_translate_error["text"] = ""
            counter_add("translate.total_chars", need)
            # 主渠道不可用、自动切到备用渠道时如实播报一次，
            # 否则用户只会看到「怎么译文风格变了」而不知道发生了什么
            notes = getattr(engine, "switch_notes", None)
            if notes:
                while notes:
                    add_event(job["id"], notes.pop(0), "warn")
            for i, value in zip(missing, outputs):
                result[i] = value
            # 个别句子反复翻不过时引擎会保留日文原文，这里如实记账并写一条日志，
            # 免得用户看到混杂日文的字幕不知道怎么回事
            missed = getattr(engine, "last_misses", None) or []
            if missed:
                kept += len(missed)
                add_event(job["id"],
                          f"有 {len(missed)} 条反复翻不过，先保留日文原文（例如：{missed[0][:24]}）",
                          "warn")
            if settings.tm_enabled:
                tm_store([(texts[i], result[i] or "") for i in missing], model)

        translated.extend(value or "" for value in result)
        done_ratio = (start + len(chunk)) / total
        update_job(job["id"], progress=70 + int(done_ratio * 29),
                   message=f"翻译 {start + len(chunk)}/{total} 条（缓存命中 {hits}）")

    final_cues = [Cue(i, c.start, c.end, text) for i, (c, text) in enumerate(zip(cues, translated), 1)]
    atomic_write(target, render_srt(final_cues), overwrite=settings.overwrite_existing)
    elapsed = time.time() - started
    counter_add("translate.wall_seconds", elapsed)
    message = "完成" if not kept else f"完成（{kept} 条保留日文原文）"
    finish(job["id"], "done", "done", message,
           zh_srt=str(target), translate_seconds=elapsed, cached_chars=cached_chars)


HANDLERS = {"extract": handle_extract, "asr": handle_asr, "translate": handle_translate}


# --------------------------------------------------------------------- pools


def _worker_count(stage: str, settings: Settings) -> int:
    return max(1, {"extract": settings.extract_workers,
                   "asr": settings.asr_workers,
                   "translate": settings.translate_workers}.get(stage, 1))


def _loop(stage: str, index: int) -> None:
    handler = HANDLERS[stage]
    while not stop_event.is_set():
        try:
            settings = current_settings()
            if index >= _worker_count(stage, settings) or blocked():
                stop_event.wait(3)
                continue
            job = claim(stage)
            if job is None:
                stop_event.wait(1.5)
                continue
            add_event(job["id"],
                      f"开始处理「{stage_label(stage)}」：{Path(job['video_path']).name}",
                      "info")
            try:
                handler(job, settings)
            except BudgetExceeded as exc:
                update_job(job["id"], status="pending", message=str(exc)[:200],
                           next_attempt_at=budget_block_until["ts"])
                add_event(job["id"], str(exc), "warn")
            except MissingConfigError as exc:
                # 没填接口/密钥：不算失败，不消耗重试次数，挂起等用户在 UI 里配好
                config_wait_reason["text"] = str(exc)[:120]
                config_wait_until["ts"] = time.time() + 300
                update_job(job["id"], status="pending", message=str(exc)[:200],
                           next_attempt_at=time.time() + 300)
                add_event(job["id"], f"[{stage}] 挂起：{exc}", "warn")
            except FatalTranslationError as exc:
                if stage == "translate":
                    last_translate_error["text"] = str(exc)[:200]
                    last_translate_error["ts"] = time.time()
                update_job(job["id"], status="failed", error=str(exc)[:500],
                           message=str(exc)[:200], finished_at=now(), stage=stage)
                add_event(job["id"], f"[{stage}] 不可重试：{exc}", "error")
            except Exception as exc:  # noqa: BLE001
                if stage == "translate":
                    last_translate_error["text"] = f"{type(exc).__name__}: {exc}"[:200]
                    last_translate_error["ts"] = time.time()
                log.exception("job %s stage %s failed", job["id"], stage)
                fail(job["id"], f"{type(exc).__name__}: {exc}", stage,
                     job["attempts"], settings.max_attempts)
        except Exception:  # noqa: BLE001  队列本身出错也要活下去
            log.exception("worker %s/%s crashed", stage, index)
            stop_event.wait(5)


def start_pools() -> None:
    if _threads:
        return
    for stage in POOL_STAGES:
        for index in range(MAX_POOL):
            thread = threading.Thread(target=_loop, args=(stage, index), daemon=True,
                                      name=f"{stage}-{index}")
            thread.start()
            _threads.append(thread)


def stop_pools(timeout: float = 5.0) -> None:
    stop_event.set()
    for thread in _threads:
        thread.join(timeout=timeout)
    _threads.clear()


def recover_stale() -> int:
    """进程重启后，把上次跑到一半的任务放回队列。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='pending', message='重启后恢复' WHERE status='running'")
        return cur.rowcount


# --------------------------------------------------------------------- enqueue


def queue_indexed(root: str | None = None, force: bool = False, limit: int = 0) -> int:
    """把已索引、条件满足的视频加入队列。

    root  限定只处理某个目录（含子目录）；None 表示整库。
    force 用户在 UI 上手动点了「开始处理」，跳过「文件已稳定 120 秒」的闸门。
    limit >0 时最多只入队这么多个（按路径顺序取前面几部），用于先拿几部试水。
    """
    settings = load_settings()
    media_root = Path(settings.media_root).expanduser().resolve()
    scope = Path(root).expanduser().resolve() if root else media_root
    if scope != media_root and not scope.is_relative_to(media_root):
        raise ValueError("目录不在媒体根目录内")

    queued = 0
    with connect() as conn:
        rows = conn.execute("SELECT * FROM media ORDER BY path").fetchall()
        for row in rows:
            if limit > 0 and queued >= limit:
                break
            video = Path(row["path"])
            if not video.is_file() or video.is_symlink():
                continue
            try:
                resolved = video.resolve()
                if not resolved.is_relative_to(scope):
                    continue
                stat = video.stat()
            except OSError:
                continue
            if not force:
                unchanged = time.time() - datetime.fromisoformat(row["updated_at"]).timestamp()
                if (row["stable_scans"] < 1 or unchanged < settings.min_file_age
                        or stat.st_size != row["size"] or stat.st_mtime != row["mtime"]
                        or time.time() - stat.st_mtime < settings.min_file_age):
                    continue
            if conn.execute("SELECT 1 FROM jobs WHERE video_path=?", (str(video),)).fetchone():
                continue
            target, _ = target_paths(video, settings.target_language)
            if target.exists() and not settings.overwrite_existing:
                # 已有中文字幕：不建任务，也不留记录。这样以后删掉字幕再扫还能重新排队。
                continue
            conn.execute(
                "INSERT INTO jobs(video_path,stage,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(video), "extract", "pending", now(), time.time()))
            queued += 1
    if queued:
        add_event(0, f"用户选择目录 {scope}：新入队 {queued} 部", "info")
    return queued


def count_pending(scope: str | None = None) -> dict:
    """给 UI 用：某目录下还有多少没入队、多少在队列里。"""
    settings = load_settings()
    media_root = Path(settings.media_root).expanduser().resolve()
    target_dir = Path(scope).expanduser().resolve() if scope else media_root
    total = queued = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT m.path AS path, j.status AS status FROM media m "
            "LEFT JOIN jobs j ON j.video_path=m.path").fetchall()
    for row in rows:
        video = Path(row["path"])
        try:
            if not video.resolve().is_relative_to(target_dir):
                continue
        except OSError:
            continue
        total += 1
        if row["status"] is not None:
            queued += 1
    return {"videos": total, "in_queue": queued, "not_queued": total - queued}


# --------------------------------------------------------------------- stats


def config_ready(settings: Settings) -> bool:
    """当前渠道是否具备发请求的条件。

    谷歌/必应免密钥，永远算就绪；deepl 要 key；openai 要地址+模型+key。
    配了备用渠道时，只要链上有一个能用就算就绪——主 key 失效不该
    把整个队列判成「没配置好」而停在那儿等人工。
    """
    def single(provider: str) -> bool:
        if provider in ("google", "bing"):
            return True
        if provider == "deepl":
            return bool(settings.deepl_api_key)
        return bool(settings.openai_base_url and settings.openai_model
                    and settings.openai_api_key)

    if single(normalize_provider(settings.translate_provider)):
        return True
    return any(single(p) for p in parse_fallback(getattr(settings, "translate_fallback", "")))


def device_summary(settings: Settings) -> dict:
    """给界面用的一行话：转写固定用 CPU，这里是实际会用的线程数。"""
    return {
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": asr.threads_for(settings.cpu_threads),
        "cpu_count": os.cpu_count() or 1,
    }


def stats() -> dict:
    with connect() as conn:
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
        by_stage = {r["stage"]: r["c"] for r in conn.execute(
            "SELECT stage, COUNT(*) c FROM jobs WHERE status IN ('pending','running') GROUP BY stage")}
        row = conn.execute(
            "SELECT COALESCE(SUM(duration),0) d, COALESCE(SUM(asr_seconds),0) a "
            "FROM jobs WHERE duration>0 AND asr_seconds>0").fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(duration),0) d FROM jobs "
            "WHERE status='pending' AND stage IN ('extract','asr')").fetchone()
        avg = conn.execute(
            "SELECT COALESCE(AVG(duration),0) a FROM jobs WHERE duration>0").fetchone()
        untimed = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status='pending' AND stage IN ('extract','asr') "
            "AND duration<=0").fetchone()["c"]
        indexed = conn.execute("SELECT COUNT(*) c FROM media").fetchone()["c"]
    settings = load_settings()

    ratio = (row["d"] / row["a"]) if row and row["a"] > 0 else 0.0
    measured_avg = float(avg["a"] or 0)
    pending_media = float(pending["d"] or 0) + untimed * measured_avg if ratio > 0 else 0.0
    eta = (pending_media / ratio) if ratio > 0 and pending_media > 0 else None

    return {
        "total": sum(by_status.values()),
        "done": by_status.get("done", 0),
        "failed": by_status.get("failed", 0),
        "skipped": by_status.get("skipped", 0),
        "running": by_status.get("running", 0),
        "pending": by_status.get("pending", 0),
        "indexed_videos": indexed,
        "by_stage": by_stage,
        "paused": pause_event.is_set(),
        "pause_reason": pause_info["reason"],
        "blocked": blocked(),
        "asr_ratio": round(ratio, 2),               # 每 1 秒实际耗时处理多少秒素材
        "pending_media_seconds": round(pending_media),
        "eta_seconds": round(eta) if eta else None,
        "chars_today": int(counter_get(f"chars:{today()}")),
        "daily_char_budget": settings.daily_char_budget,
        "chars_total": int(counter_get("translate.total_chars")),
        "workers": {"extract": settings.extract_workers, "asr": settings.asr_workers,
                    "translate": settings.translate_workers},
        "translate_api": {**limiter.snapshot(),
                          "last_error": last_translate_error["text"],
                          "last_error_age": (int(time.time() - last_translate_error["ts"])
                                             if last_translate_error["ts"] else -1)},
        "config_ready": config_ready(settings),
        "translate_provider": normalize_provider(settings.translate_provider),
        # 备用渠道串（空 = 没配降级）。UI 拿它判断要不要提醒「主渠道挂了」
        "translate_fallback": getattr(settings, "translate_fallback", "") or "",
        # 转写固定 CPU+int8，这里只是实际线程数，方便对照设置
        "asr_device": device_summary(settings),
    }
