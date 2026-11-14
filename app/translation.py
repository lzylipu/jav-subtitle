"""OpenAI-compatible Japanese-to-Chinese subtitle translation."""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from .ratelimit import limiter

SYSTEM_PROMPT = """你是电影字幕翻译。把日文对白翻译成自然简洁的简体中文，保留语气，不要漏译。
只返回 JSON 对象，格式为 {"lines":[{"id":1,"text":"译文"}]}。
每个输入 id 必须出现一次、顺序不变、不得为空；不要解释，不要附加原文。"""


class TranslationError(RuntimeError):
    """可重试的翻译失败（网络、5xx、429、格式不符）。"""


class RateLimitedError(TranslationError):
    """429：已经触发全局冷却，重试前会先等到冷却结束。"""


class FatalTranslationError(RuntimeError):
    """不可重试：密钥错误、模型不存在、请求被拒。"""


class MissingConfigError(FatalTranslationError):
    """没填接口地址或密钥。不消耗重试次数，直接挂起等用户配置。"""


def _split_batch(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ...]:
    mid = len(items) // 2
    return items[:mid], items[mid:]


class OpenAITranslator:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 180,
                 retries: int = 4, rpm: int = 20, min_interval: float = 3.0,
                 backoff_429: int = 120, stop_event: Any = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = max(1, retries)
        self.rpm = rpm
        self.min_interval = min_interval
        self.backoff_429 = backoff_429
        self.stop_event = stop_event
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ public
    def translate(self, cues: list[Any]) -> list[str]:
        """把一批 Cue 翻成中文，返回等长列表。失败会自动退避重试并二分拆分。"""
        if not cues:
            return []
        if not self.base_url or not self.model:
            raise MissingConfigError("翻译接口未配置：请在设置页填写接口地址与模型")
        items = [{"id": i + 1, "text": c.text} for i, c in enumerate(cues)]
        return self._translate_items(items)

    def _translate_items(self, items: list[dict[str, Any]]) -> list[str]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            # 每一次真正发请求前都要过全局限流闸门
            if not limiter.acquire(self.rpm, self.min_interval, self.stop_event):
                raise TranslationError("队列正在停止，放弃本次翻译")
            try:
                return self._request(items)
            except RateLimitedError as exc:
                last_error = exc
                # 冷却由 limiter 统一推进，这里只做短等待避免忙等
                time.sleep(min(2 ** attempt, 10))
            except FatalTranslationError:
                raise
            except TranslationError as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 20))
        # 整批反复失败时二分拆分，隔离个别毒字幕
        if len(items) > 1:
            left, right = _split_batch(items)
            return self._translate_items(left) + self._translate_items(right)
        raise TranslationError(f"翻译失败：{last_error}")

    # ----------------------------------------------------------------- private
    def _request(self, items: list[dict[str, Any]]) -> list[str]:
        body = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TranslationError(f"网络错误：{exc}") from exc

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            seconds = limiter.note_429(retry_after, self.backoff_429)
            raise RateLimitedError(f"上游限流 429，全体冷却 {int(seconds)} 秒")
        if response.status_code in (401, 403):
            raise FatalTranslationError(f"翻译接口鉴权失败 HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code in (500, 502, 503, 504):
            raise TranslationError(f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            text = response.text[:300]
            if "model_not_found" in text or "no available channel" in text.lower():
                raise FatalTranslationError(f"模型不可用：{text}")
            raise TranslationError(f"HTTP {response.status_code}: {text}")

        limiter.note_success()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TranslationError("翻译接口响应缺少 choices.message.content") from exc
        return self._parse_lines(content, len(items))

    def _parse_lines(self, content: str, expected: int) -> list[str]:
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\n|```$", "", content).strip()
        try:
            parsed = json.loads(content)
            lines = parsed["lines"]
            if not isinstance(lines, list) or len(lines) != expected:
                raise ValueError("翻译条数不一致")
            for i, item in enumerate(lines, 1):
                if (not isinstance(item, dict) or type(item.get("id")) is not int
                        or item["id"] != i or not isinstance(item.get("text"), str)
                        or not item["text"].strip()):
                    raise ValueError("翻译编号、顺序或正文不正确")
            return [item["text"].strip() for item in lines]
        except (ValueError, KeyError, TypeError) as exc:
            raise TranslationError("翻译响应不是完整且有序的 JSON 字幕列表") from exc


def _parse_retry_after(raw: str | None) -> float | None:
    """Retry-After 可能是秒数，也可能是 HTTP 日期。只认秒数，解析不了就返回 None。"""
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
