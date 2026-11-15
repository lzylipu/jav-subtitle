"""Application configuration persisted in SQLite.

设计原则（v3）：
- 所有可调参数都在 Web UI 里能改，落库即生效，不用改 .env 也不用重启容器。
- 默认值一律保守（并发 1），用户自己往上调。宁可慢，不要打爆中转站。
- 密钥（OpenAI key / AI 接入 token）只存数据库，不进仓库、不进 .env、不写镜像。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "pipeline.db")))
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", str(DATA_DIR / "audio")))
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".m2ts", ".mov", ".rmvb")
STAGES = ("extract", "asr", "translate")


@dataclass
class Settings:
    # 目录与语言
    media_root: str = "/media"
    source_language: str = "ja"
    target_language: str = "zh-CN"

    # 转写（固定 CPU + int8：这个程序不做显卡加速，理由见 README）
    whisper_model: str = "small"
    # 0 = 自动取 min(4, 核数)
    cpu_threads: int = 0
    beam_size: int = 1
    # 0/1 = 关闭批推理。实测（2 vCPU）：批推理快 34%，但会把 30 句对白并成 5 条长字幕，
    # 时间轴粒度毁掉，不适合做字幕。要速度不要粒度的场景再开。
    asr_batch_size: int = 1

    # 并行度：三个阶段各自排队、互不阻塞。默认全部 1——先把队列跑顺，再谈提速。
    extract_workers: int = 1
    asr_workers: int = 1
    translate_workers: int = 1

    # 翻译
    # 渠道：openai / google / bing / deepl。前两个免密钥，deepl 要 key。
    translate_provider: str = "openai"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    google_api_key: str = ""      # 留空用内置公开 key，失效会自动去谷歌前端抓新的
    deepl_api_key: str = ""       # 免费版以 :fx 结尾
    deepl_api_url: str = ""       # 留空按 key 后缀自动选 free / pro 端点
    # 备用渠道（逗号分隔，例如 "google,bing"）。主渠道 key 失效 / 额度用光 /
    # 端点不通时自动切过去，任务照跑，不必等人来改设置。
    # 只对「整库都跑不动」的错误切换；偶发网络错和限流仍由各渠道自己重试。
    translate_fallback: str = ""
    # 实测（同一批 120 条真实字幕，走中转站）：40 条一批 2.6 秒/条，
    # 20 条一批 5.8 秒/条 —— 上游每次请求本身就要几十秒，批越小越亏。
    # 万一某一批模型数不清条数，引擎自己会拆成两小批重发，不用靠小批次兜底。
    translation_batch_size: int = 40
    translate_timeout: int = 180
    translate_retries: int = 4

    # 限流闸门：这是「不要打流中转站」的全部手段，默认偏保守
    translate_rpm: int = 20               # 每分钟最多几次请求，0 = 不限
    translate_min_interval: float = 3.0   # 两次请求之间至少隔几秒（不突发）
    translate_429_backoff: int = 120      # 收到 429 后全局冷却多少秒

    # 成本闸门：0 表示不限制，单位是「每天送去翻译的字数」
    daily_char_budget: int = 0
    tm_enabled: bool = True

    # 扫描
    min_file_age: int = 120
    # 默认关闭：扫描只建索引，绝不偷偷把整个库排队。
    # 想「一放进去就自己跑」的人再打开。
    auto_enqueue: bool = False
    schedule_enabled: bool = False
    schedule_hour: int = 3
    schedule_minute: int = 0

    # 其他
    keep_audio: bool = False
    keep_japanese_srt: bool = True
    overwrite_existing: bool = False
    max_attempts: int = 3

    # AI 接入：token 由服务首次启动时随机生成，只存数据库
    api_token: str = ""
    api_token_readonly: bool = False
    web_public_url: str = ""              # 例如 http://10.10.0.5:8000，供 AI 侧拼地址

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for field in ("openai_api_key", "google_api_key", "deepl_api_key"):
            data[field] = "********" if data.get(field) else ""
        return data

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def settings_from_env() -> Settings:
    """环境变量只负责首次启动的种子值，之后以数据库为准。"""
    values = Settings()
    mapping = {
        "MEDIA_ROOT": "media_root",
        "SOURCE_LANGUAGE": "source_language",
        "TARGET_LANGUAGE": "target_language",
        "WHISPER_MODEL": "whisper_model",
        "CPU_THREADS": "cpu_threads",
        "ASR_WORKERS": "asr_workers",
        "EXTRACT_WORKERS": "extract_workers",
        "TRANSLATE_WORKERS": "translate_workers",
        "TRANSLATE_PROVIDER": "translate_provider",
        "OPENAI_BASE_URL": "openai_base_url",
        "OPENAI_API_KEY": "openai_api_key",
        "OPENAI_MODEL": "openai_model",
        "GOOGLE_API_KEY": "google_api_key",
        "DEEPL_API_KEY": "deepl_api_key",
        "DEEPL_API_URL": "deepl_api_url",
        "TRANSLATE_FALLBACK": "translate_fallback",
        "TRANSLATION_BATCH_SIZE": "translation_batch_size",
        "DAILY_CHAR_BUDGET": "daily_char_budget",
        "TRANSLATE_RPM": "translate_rpm",
        "TRANSLATE_MIN_INTERVAL": "translate_min_interval",
        "WEB_PUBLIC_URL": "web_public_url",
    }
    for env_name, attr in mapping.items():
        if env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        current = getattr(values, attr)
        if isinstance(current, bool):
            value: Any = raw.lower() in {"1", "true", "yes", "on"}
        elif isinstance(current, int):
            value = int(raw)
        elif isinstance(current, float):
            value = float(raw)
        elif isinstance(current, str):
            value = raw
        else:  # pragma: no cover
            value = raw
        setattr(values, attr, value)
    return values
