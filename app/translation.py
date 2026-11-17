"""字幕翻译引擎。

四个渠道，行为统一：`translate(cues) -> list[str]`，输出与输入等长、顺序一一对应。

- openai —— 任意 OpenAI 兼容接口（中转站 / 本地模型），要 base_url + model + key
- google —— 谷歌网页翻译接口（免密钥），translate-pa.googleapis.com
- bing   —— 必应网页翻译接口（免密钥），edge.microsoft.com
- deepl  —— DeepL 官方 API，需要 key（免费版 key 以 :fx 结尾）

谷歌/必应都是「一次提交一组句子、原样返回一组译文」的批量接口，天然按行对齐，
比 LLM 的 JSON 约束更不容易乱序。代价是机翻质量不如大模型——这不影响管线，
只是同一批字幕换个引擎而已。

参考实现来自 Traduzir-paginas-web（TWPT）对这些免费接口的调用方式。
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlencode
from typing import Any

import httpx

from .ratelimit import limiter

SYSTEM_PROMPT = """你是电影字幕翻译。把日文对白翻译成自然简洁的简体中文，保留语气，不要漏译。
只返回 JSON 对象，格式为 {"lines":[{"id":1,"text":"译文"}]}。
输入几条就必须返回几条：每个输入 id 出现且只出现一次，顺序不变，不得合并、不得省略、不得为空。
返回前自检一遍条数与 id 是否与输入一致。不要解释，不要 markdown 代码块，不要附加原文。"""

PROVIDERS = ("openai", "google", "bing", "deepl")

PROVIDER_LABELS = {
    "openai": "OpenAI 兼容接口（大模型，质量最好）",
    "google": "谷歌翻译（免费，无需密钥）",
    "bing": "必应翻译（免费，无需密钥）",
    "deepl": "DeepL 官方 API（需 key）",
}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 谷歌网页翻译接口的 key 从前端公开 JS 动态获取，不在源码或 Git 历史里
# 保存任何 key-shaped 字面量。若前端结构变化，可通过设置页显式配置
# google_api_key 覆盖。
GOOGLE_AUTH_JS = ("https://translate.googleapis.com/_/translate_http/_/js/"
                  "k=translate_http.tr.en_US.YusFYy3P_ro.O/am=AAg/d=1/exm=el_conf/"
                  "ed=1/rs=AN8SPfq1Hb8iJRleQqQc8zhdzXmF9E56eQ/m=el_main")

_LANG_ALIASES = {
    "google": {"zh-cn": "zh-CN", "zh-tw": "zh-TW", "zh": "zh-CN", "ja": "ja",
               "en": "en", "ko": "ko"},
    "bing": {"zh-cn": "zh-Hans", "zh-tw": "zh-Hant", "zh": "zh-Hans", "ja": "ja",
             "en": "en", "ko": "ko"},
    "deepl": {"zh-cn": "ZH-HANS", "zh-tw": "ZH-HANT", "zh": "ZH-HANS",
              "ja": "JA", "en": "EN", "ko": "KO"},
}


class TranslationError(RuntimeError):
    """可重试的翻译失败（网络、5xx、429、格式不符）。"""


class RateLimitedError(TranslationError):
    """429：已经触发全局冷却，重试前会先等到冷却结束。"""


class FatalTranslationError(RuntimeError):
    """不可重试：密钥错误、模型不存在、请求被拒。"""


class ShapeError(TranslationError):
    """上游返回的条数和发出去的对不上。

    这类错误**不做二分拆分重试**：拆开之后每段各回一条，正好能拼出"看似成功、
    实际错位"的字幕——比直接失败危险得多。
    """


class MissingConfigError(FatalTranslationError):
    """没填接口地址或密钥。不消耗重试次数，直接挂起等用户配置。"""


# --------------------------------------------------------------------- helpers


def clean_secret(raw: str | None) -> str:
    """把用户粘进来的密钥收拾干净。

    从网页/聊天里复制 key 常带上首尾空白、引号、甚至整段 "Bearer sk-xxx"，
    直接塞进 Authorization 头会被上游判成无效 token（401）。

    还有一类更隐蔽的：复制时把页面上表示「已复制」的对勾 ✓ 一起带进来，
    httpx 组 header 时按 ascii 编码，会直接抛
    ``UnicodeEncodeError: 'ascii' codec can't encode character '\\u2713'``，
    看着像程序坏了，其实只是 key 里夹了个符号。所以非 ASCII 字符一律剔除。
    """
    if not raw:
        return ""
    value = raw.strip().strip('"').strip("'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if value.lower().startswith("deepl-auth-key "):
        value = value[15:].strip()
    # 只留可打印 ASCII：0x21–0x7E
    return "".join(ch for ch in value if "!" <= ch <= "~")


def normalize_provider(raw: str | None) -> str:
    name = (raw or "").strip().lower()
    return name if name in PROVIDERS else "openai"


def cache_namespace(provider: str, model: str = "") -> str:
    """翻译记忆的命名空间。换渠道必须换 namespace，否则机翻结果会污染大模型缓存。"""
    provider = normalize_provider(provider)
    if provider == "openai":
        return f"openai:{model or 'default'}"
    return provider


def _lang(provider: str, code: str) -> str:
    table = _LANG_ALIASES.get(provider, {})
    return table.get((code or "").strip().lower(), code or "")


# 二分拆分最多往下拆几层（40 条 → 20 → 10）。再往下交给逐条抢救。
SPLIT_DEPTH = 2
# 一批全部失败后逐条抢救的上限。超过就别硬试了，直接留原文。
SALVAGE_LIMIT = 60
# 连续这么多次单条也失败，判定上游整体故障，停止抢救
SALVAGE_GIVEUP = 3


def _split_batch(items: list[Any]) -> tuple[list[Any], list[Any]]:
    mid = len(items) // 2
    return items[:mid], items[mid:]


def _parse_retry_after(raw: str | None) -> float | None:
    """Retry-After 可能是秒数，也可能是 HTTP 日期。只认秒数，解析不了就返回 None。"""
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ----------------------------------------------------------------- base engine


class BaseTranslator:
    """负责重试、限流、空行对齐、二分拆分；子类只实现 `_request`。"""

    name = "base"

    def __init__(self, timeout: int = 180, retries: int = 4, rpm: int = 20,
                 min_interval: float = 3.0, backoff_429: int = 120,
                 stop_event: Any = None):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.rpm = rpm
        self.min_interval = min_interval
        self.backoff_429 = backoff_429
        self.stop_event = stop_event
        self.client = httpx.Client(timeout=timeout)
        # 上一批里实在翻不动、只能保留原文的句子。pipeline 拿它写日志给用户看。
        self.last_misses: list[str] = []

    def close(self) -> None:
        self.client.close()

    # -- 子类需要实现 ------------------------------------------------------
    def ready(self) -> bool:
        return True

    def missing_message(self) -> str:
        return "翻译接口未配置"

    def auth_hint(self) -> str:
        """401/403 时附加到报错后面的自查信息。子类按需覆盖，不许回显密钥。"""
        return ""

    def key_report(self) -> str:
        """描述密钥形状（长度、后缀、打哪个端点），绝不回显密钥本身。"""
        return ""

    def usage(self) -> dict[str, Any]:
        """当月用量 / 额度。只有带额度概念的渠道（DeepL）覆盖它。"""
        return {"ok": False, "detail": "该渠道没有额度查询接口"}

    def _request(self, texts: list[str]) -> list[str]:
        raise NotImplementedError

    # -- 公共入口 ----------------------------------------------------------
    def translate(self, cues: list[Any]) -> list[str]:
        """把一批 Cue 翻成目标语言，返回等长列表。"""
        if not cues:
            return []
        return self.translate_texts([c.text for c in cues])

    def translate_texts(self, texts: list[str]) -> list[str]:
        """翻译纯文本列表。空行/纯空白行原样留空，不发给上游。"""
        if not texts:
            return []
        if not self.ready():
            raise MissingConfigError(self.missing_message())
        keep = [i for i, t in enumerate(texts) if (t or "").strip()]
        if not keep:
            return ["" for _ in texts]
        payload = [texts[i] for i in keep]
        self.last_misses = []
        try:
            outputs = self._run(payload)
        except FatalTranslationError:
            # 401 / key 失效 / 额度用完：整库都跑不动，必须冒泡让任务停下等人处理
            raise
        except TranslationError as exc:
            # 整批救不回来（个别毒字幕反复不过）时逐条抢救。
            # 宁可整部片里有几条留日文，也不要几千条字幕全部作废。
            self.last_error = str(exc)
            outputs = self._salvage(payload)
            if len(self.last_misses) >= len(payload):
                # 一条都没救回来 —— 这是上游整体故障，绝不能当成功写出去，
                # 否则用户会拿到一份全是日文的「完成」字幕
                raise TranslationError(self.last_error or "翻译整批失败")
        result = ["" for _ in texts]
        for pos, i in enumerate(keep):
            result[i] = outputs[pos] if pos < len(outputs) else ""
        return result

    def _salvage(self, texts: list[str]) -> list[str]:
        """逐条重试，翻不动的原样返回日文。

        连续 `SALVAGE_GIVEUP` 条都失败就认为上游整体挂了，剩下的全部留原文，
        别在限流闸门前一条条耗上几十分钟。
        """
        out: list[str] = []
        streak = 0
        for idx, text in enumerate(texts[:SALVAGE_LIMIT]):
            try:
                out.extend(self._run([text]))
                streak = 0
            except FatalTranslationError:
                raise
            except TranslationError:
                out.append(text)
                self.last_misses.append(text)
                streak += 1
                if streak >= SALVAGE_GIVEUP:
                    out.extend(texts[idx + 1:])
                    self.last_misses.extend(texts[idx + 1:])
                    break
        if len(texts) > SALVAGE_LIMIT:
            out.extend(texts[SALVAGE_LIMIT:])
            self.last_misses.extend(texts[SALVAGE_LIMIT:])
        return out

    def _run(self, texts: list[str], depth: int = 0) -> list[str]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            # 每一次真正发请求前都要过全局限流闸门
            if not limiter.acquire(self.rpm, self.min_interval, self.stop_event):
                raise TranslationError("队列正在停止，放弃本次翻译")
            try:
                out = self._request(texts)
                if len(out) != len(texts):
                    raise ShapeError(
                        f"返回条数不符：发了 {len(texts)} 条，回来 {len(out)} 条")
                return out
            except ShapeError as exc:
                # 条数不对时重发同一批基本还是不对（模型这轮就是没数清），
                # 直接往下拆成两小批，成功率明显更高
                last_error = exc
                break
            except RateLimitedError as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 10))
            except FatalTranslationError:
                raise
            except UnicodeEncodeError as exc:
                # 请求头里混进了非 ASCII（多半是 key/地址里带了 ✓ 之类的符号）。
                # 原样抛出去用户只看到一句 Python 堆栈，根本不知道要改哪里。
                raise FatalTranslationError(
                    "接口地址或密钥里夹了非 ASCII 字符（比如复制时带上的 ✓、全角空格），"
                    "只留字母数字和半角符号重填一次即可。原始错误：" + str(exc)) from exc
            except TranslationError as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 20))
        # 整批反复失败时二分拆分，隔离个别毒字幕。
        # 但只拆 SPLIT_DEPTH 层：上游整体故障时无限二分会把限流闸门耗死
        # （40 条一批能打出 79 次请求，用户看到的就是「一直在发、就是不出字幕」）。
        if len(texts) > 1 and depth < SPLIT_DEPTH:
            left, right = _split_batch(texts)
            return self._run(left, depth + 1) + self._run(right, depth + 1)
        raise TranslationError(f"翻译失败：{last_error}")

    # -- 公共工具 ----------------------------------------------------------
    def _check_status(self, response: httpx.Response) -> None:
        code = response.status_code
        if code == 429:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            seconds = limiter.note_429(retry_after, self.backoff_429)
            raise RateLimitedError(f"上游限流 429，全体冷却 {int(seconds)} 秒")
        if code in (500, 502, 503, 504):
            raise TranslationError(f"HTTP {code}: {response.text[:200]}")
        if code >= 400:
            text = response.text[:300]
            if code in (401, 403):
                hint = self.auth_hint()
                raise FatalTranslationError(
                    f"翻译接口鉴权失败 HTTP {code}: {text}" + (f"\n自查：{hint}" if hint else ""))
            if code == 456:
                raise FatalTranslationError(f"DeepL 额度已用完：{text}")
            if "model_not_found" in text or "no available channel" in text.lower():
                raise FatalTranslationError(f"模型不可用：{text}")
            raise TranslationError(f"HTTP {code}: {text}")
        limiter.note_success()


# --------------------------------------------------------------------- OpenAI


class OpenAITranslator(BaseTranslator):
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 180,
                 retries: int = 4, rpm: int = 20, min_interval: float = 3.0,
                 backoff_429: int = 120, stop_event: Any = None):
        super().__init__(timeout, retries, rpm, min_interval, backoff_429, stop_event)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = clean_secret(api_key)
        self.model = model or ""

    # 兼容老调用方式：translate(cues) 已在基类实现
    def ready(self) -> bool:
        return bool(self.base_url and self.model)

    def missing_message(self) -> str:
        return "翻译接口未配置：请在设置页填写接口地址与模型"

    def _request(self, texts: list[str]) -> list[str]:
        items = [{"id": i + 1, "text": t} for i, t in enumerate(texts)]
        # 条数写进用户消息，比只放在 system 里管用；max_tokens 按输入量给足，
        # 免得长批次被试玩性默认值截断成半截 JSON（就是「条数不对」的主因之一）
        head = f"下面共 {len(texts)} 条，必须返回 {len(texts)} 条。\n"
        body = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": max(2048, min(16384, len("".join(texts)) * 3 + 1024)),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": head + json.dumps(items, ensure_ascii=False)},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        try:
            response = self.client.post(f"{self.base_url}/chat/completions",
                                        json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TranslationError(f"网络错误：{exc}") from exc

        self._check_status(response)
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TranslationError("翻译接口响应缺少 choices.message.content") from exc
        return self._parse_lines(content, len(texts))

    @staticmethod
    def _pick_lines(parsed: Any) -> list | None:
        """模型有时会换外壳（{"lines":..} / {"translations":..} / 裸数组），都认。"""
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("lines", "translations", "data", "result"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
        return None

    def _parse_lines(self, content: str, expected: int) -> list[str]:
        content = (content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\n|```$", "", content).strip()
        parsed: Any
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            # 半截 JSON（被截断）或夹杂解释文字：属于这一批没交好货，可拆批重试
            raise TranslationError("翻译响应不是合法的 JSON") from exc
        lines = self._pick_lines(parsed)
        if lines is None:
            raise TranslationError("翻译响应缺少 lines 数组")
        if len(lines) != expected:
            raise ShapeError(f"返回条数不符：要 {expected} 条，回来 {len(lines)} 条")
        texts: list[str] = []
        for i, item in enumerate(lines, 1):
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                got = item.get("id")
                # 有的模型把 id 写成 "1" 而不是 1，只要顺序对就认
                if got is not None and str(got).strip() != str(i):
                    raise ShapeError(f"第 {i} 条的 id 是 {got!r}，顺序乱了")
                text = item.get("text")
            else:
                raise ShapeError(f"第 {i} 条既不是字符串也不是对象")
            if not isinstance(text, str) or not text.strip():
                raise ShapeError(f"第 {i} 条译文为空")
            texts.append(text.strip())
        return texts


# --------------------------------------------------------------------- Google


class GoogleTranslator(BaseTranslator):
    """谷歌网页翻译接口。免密钥，一次提交一组句子、原样返回一组译文。"""

    name = "google"
    ENDPOINT = "https://translate-pa.googleapis.com/v1/translateHtml"
    MAX_ITEMS = 50

    def __init__(self, source_language: str = "ja", target_language: str = "zh-CN",
                 api_key: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.source_language = _lang("google", source_language)
        self.target_language = _lang("google", target_language)
        self.api_key = clean_secret(api_key)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json+protobuf",
                "X-goog-api-key": self.api_key,
                "User-Agent": BROWSER_UA}

    def _refresh_key(self) -> bool:
        """从谷歌前端公开 js 里获取当前 key。"""
        try:
            resp = self.client.get(GOOGLE_AUTH_JS, headers={"User-Agent": BROWSER_UA})
            match = re.search(r'["\']x-goog-api-key["\']\s*:\s*["\'](\w{39})["\']',
                              resp.text or "", re.I)
        except httpx.HTTPError:
            return False
        if match:
            self.api_key = match.group(1)
            return True
        return False

    def _request(self, texts: list[str]) -> list[str]:
        if len(texts) > self.MAX_ITEMS:
            head, tail = texts[:self.MAX_ITEMS], texts[self.MAX_ITEMS:]
            return self._run(head) + self._run(tail)
        body = [[texts, self.source_language, self.target_language], "te"]
        try:
            # 注意：这个接口吃的是 JSON，但不能当成 application/json 发，
            # 上游只认 application/json+protobuf 的排版体。
            response = self.client.post(
                self.ENDPOINT, headers=self._headers(),
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except httpx.HTTPError as exc:
            raise TranslationError(f"网络错误：{exc}") from exc

        if response.status_code in (401, 403) and self._refresh_key():
            try:
                response = self.client.post(
                    self.ENDPOINT, headers=self._headers(),
                    content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
            except httpx.HTTPError as exc:
                raise TranslationError(f"网络错误：{exc}") from exc

        self._check_status(response)
        try:
            data = response.json()
            return [str(x) for x in data[0]]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError("谷歌翻译响应格式不是预期的 [译文字典]") from exc


# ----------------------------------------------------------------------- Bing


class BingTranslator(BaseTranslator):
    """必应网页翻译接口。免密钥，body 是句子数组，返回同序数组。"""

    name = "bing"
    ENDPOINT = "https://edge.microsoft.com/translate/translatetext"
    MAX_ITEMS = 50

    def __init__(self, source_language: str = "ja", target_language: str = "zh-CN",
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.source_language = _lang("bing", source_language)
        self.target_language = _lang("bing", target_language)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "User-Agent": BROWSER_UA}

    def _request(self, texts: list[str]) -> list[str]:
        if len(texts) > self.MAX_ITEMS:
            head, tail = texts[:self.MAX_ITEMS], texts[self.MAX_ITEMS:]
            return self._run(head) + self._run(tail)
        params = {"isEnterpriseClient": "false",
                  "from": self.source_language, "to": self.target_language}
        try:
            response = self.client.post(self.ENDPOINT, params=params,
                                        headers=self._headers(), json=texts)
        except httpx.HTTPError as exc:
            raise TranslationError(f"网络错误：{exc}") from exc

        self._check_status(response)
        try:
            data = response.json()
            out = []
            for item in data:
                translated = item["translations"][0]["text"]
                out.append(str(translated))
            return out
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError("必应翻译响应格式不是预期的 [ {translations} ]") from exc


# ---------------------------------------------------------------------- DeepL


class DeepLTranslator(BaseTranslator):
    """DeepL 官方 API。免费版 key 以 :fx 结尾，走 api-free；否则走付费端点。"""

    name = "deepl"
    FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
    PRO_ENDPOINT = "https://api.deepl.com/v2/translate"
    MAX_ITEMS = 50

    def __init__(self, api_key: str, source_language: str = "ja",
                 target_language: str = "zh-CN", api_url: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.api_key = clean_secret(api_key)
        self.source_language = _lang("deepl", source_language)
        self.target_language = _lang("deepl", target_language)
        if api_url:
            self.endpoint = api_url.strip()
        elif self.api_key.endswith(":fx"):
            self.endpoint = self.FREE_ENDPOINT
        else:
            self.endpoint = self.PRO_ENDPOINT

    def ready(self) -> bool:
        return bool(self.api_key)

    def missing_message(self) -> str:
        return ("DeepL 没填密钥：到设置页把 DeepL API key 填上"
                "（免费版注册地址 deepl.com/pro-api，key 以 :fx 结尾）")

    def key_report(self) -> str:
        """把 key 的形状描述出来（绝不回显 key 本身）。403 时全靠这个判断。"""
        key = self.api_key
        head = f"{len(key)} 位"
        tail = "以 :fx 结尾（免费版 key 的形状）" if key.endswith(":fx") \
            else "不以 :fx 结尾（免费版 key 一定带 :fx）"
        return f"密钥{head}、{tail}，本次请求打到 {self.endpoint}"

    def auth_hint(self) -> str:
        """403 是「key 本身无效/订阅没生效」，跟字幕条数、字数完全无关。

        实测：把一条 20 字的短句发过去同样 403，所以别去调批大小。
        """
        return (self.key_report()
                + "。DeepL 的 403 只表示 key 无效或 API 订阅未生效，"
                  "与一次发多少条、多少字无关（单句也是 403）。"
                  "请到 DeepL 账号「API Keys」页确认：订阅是 API Free、"
                  "key 是完整复制的（免费版一定以 :fx 结尾）、没有被停用。")

    def usage(self) -> dict[str, Any]:
        """查当月已用字符 / 上限。key 无效时同样会 403，正好用来区分「没权限」和「没额度」。"""
        url = self.endpoint.rsplit("/v2/", 1)[0] + "/v2/usage"
        response = self.client.get(url, headers={
            "Authorization": "DeepL-Auth-Key " + self.api_key})
        if response.status_code != 200:
            return {"ok": False, "status": response.status_code,
                    "detail": response.text[:200]}
        data = response.json()
        return {"ok": True, "used": data.get("character_count"),
                "limit": data.get("character_limit")}

    def _request(self, texts: list[str]) -> list[str]:
        if len(texts) > self.MAX_ITEMS:
            head, tail = texts[:self.MAX_ITEMS], texts[self.MAX_ITEMS:]
            return self._run(head) + self._run(tail)
        # DeepL 要的是「同一个 text 键重复多次」的表单，dict 表达不了，自己拼
        fields: list[tuple[str, str]] = [("text", t) for t in texts]
        fields.append(("target_lang", self.target_language))
        if self.source_language:
            fields.append(("source_lang", self.source_language))
        headers = {"Authorization": "DeepL-Auth-Key " + self.api_key,
                   "Content-Type": "application/x-www-form-urlencoded"}
        try:
            response = self.client.post(self.endpoint, headers=headers,
                                        content=urlencode(fields).encode("utf-8"))
        except httpx.HTTPError as exc:
            raise TranslationError(f"网络错误：{exc}") from exc

        self._check_status(response)
        try:
            out = [str(t["text"]) for t in response.json()["translations"]]
            return out
        except (ValueError, KeyError, TypeError) as exc:
            raise TranslationError("DeepL 响应格式不是预期的 {translations}") from exc


# -------------------------------------------------------------------- 渠道降级


def parse_fallback(raw: str | None) -> list[str]:
    """把 "google,bing" 这种设置解析成渠道名列表，去掉主渠道和重复项。"""
    out: list[str] = []
    for piece in (raw or "").replace("，", ",").split(","):
        name = normalize_provider(piece) if piece.strip() else ""
        if piece.strip() and piece.strip().lower() in PROVIDERS and name not in out:
            out.append(name)
    return out


class ChainTranslator:
    """主渠道挂了自动换备用渠道。

    为什么需要它：DeepL 的 key 一失效，整库 5 个任务全在第一批 403 停摆，
    得等人来改设置。备用渠道就是为了「机器自己扛过去」。

    切换范围刻意收窄——只对「整库都跑不动」的错切：
      · 鉴权失败 / key 失效（401、403）
      · 额度用光（456）
      · 完全没配置（MissingConfigError）
    偶发网络错、429 限流、条数不符仍由各渠道自己重试，切来切去只会更乱。
    """

    def __init__(self, entries: list[tuple[str, Any]], primary: str = ""):
        self.entries = entries                       # [(渠道名, 引擎)]
        self.primary = primary or (entries[0][0] if entries else "")
        self.index = 0
        self.last_misses: list[str] = []
        self.switch_notes: list[str] = []            # pipeline 拿去写日志给人看
        self._announced: set[str] = set()

    # -- 状态 --------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.active_name

    @property
    def active_name(self) -> str:
        return self.entries[self.index][0] if self.entries else ""

    @property
    def active(self) -> Any:
        return self.entries[self.index][1] if self.entries else None

    @property
    def degraded(self) -> bool:
        """当前是不是在用备用渠道跑（主渠道没在干活）。"""
        return bool(self.entries) and self.index > 0

    def ready(self) -> bool:
        return bool(self.entries)

    def missing_message(self) -> str:
        return "没有任何可用的翻译渠道"

    def key_report(self) -> str:
        engine = self.active
        return engine.key_report() if engine is not None else ""

    def usage(self) -> dict:
        engine = self.active
        return engine.usage() if engine is not None else {"ok": False, "detail": "没有激活的渠道"}

    def close(self) -> None:
        for _, engine in self.entries:
            try:
                engine.close()
            except Exception:  # noqa: BLE001 - 关连接失败不影响结果
                pass

    # -- 翻译 --------------------------------------------------------------
    def translate(self, cues: list[Any]) -> list[str]:
        if not cues:
            return []
        return self.translate_texts([c.text for c in cues])

    def translate_texts(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        errors: list[str] = []
        start = self.index
        for i in range(start, len(self.entries)):
            name, engine = self.entries[i]
            try:
                outputs = engine.translate_texts(texts)
            except (FatalTranslationError, MissingConfigError) as exc:
                # 整库都跑不动的错：记下来，换下一个渠道。同一渠道只播报一次，
                # 免得几百批任务把日志刷满
                errors.append(f"{name}：{exc}")
                self.index = i + 1
                if name not in self._announced:
                    self._announced.add(name)
                    nxt = self.entries[i + 1][0] if i + 1 < len(self.entries) else "（没有备用渠道了）"
                    # 日志只留第一行：自查提示那种长文案留给「测试当前渠道」和任务报错，
                    # 播报写成一行才不会把日志页刷成一面墙
                    brief = str(exc).splitlines()[0][:160]
                    self.switch_notes.append(f"渠道「{PROVIDER_LABELS.get(name, name)}」不可用，"
                                            f"已自动切到「{PROVIDER_LABELS.get(nxt, nxt)}」：{brief}")
                continue
            self.index = i
            self.last_misses = getattr(engine, "last_misses", []) or []
            return outputs
        raise FatalTranslationError(
            "所有翻译渠道都不可用：" + "；".join(
                line.splitlines()[0][:120] for line in errors))


def build_translator(provider: str, settings: Any, stop_event: Any = None) -> BaseTranslator:
    """按设置里的渠道造一个引擎。settings 可以是 Settings 对象或 dict。

    配了 translate_fallback 就返回 ChainTranslator：主渠道整库不可用时自动
    切到备用渠道，任务不再停在第一批等人改设置。
    """
    def get(name: str, default: Any = "") -> Any:
        if isinstance(settings, dict):
            return settings.get(name, default)
        return getattr(settings, name, default)

    name = normalize_provider(provider)
    common: dict[str, Any] = dict(
        timeout=int(get("translate_timeout", 180) or 180),
        retries=int(get("translate_retries", 4) or 4),
        rpm=int(get("translate_rpm", 20) or 0),
        min_interval=float(get("translate_min_interval", 3.0) or 0.0),
        backoff_429=int(get("translate_429_backoff", 120) or 120),
        stop_event=stop_event,
    )
    source = str(get("source_language", "ja") or "ja")
    target = str(get("target_language", "zh-CN") or "zh-CN")

    def make(which: str) -> BaseTranslator:
        if which == "google":
            return GoogleTranslator(source, target,
                                    api_key=str(get("google_api_key", "") or ""), **common)
        if which == "bing":
            return BingTranslator(source, target, **common)
        if which == "deepl":
            return DeepLTranslator(str(get("deepl_api_key", "") or ""), source, target,
                                   api_url=str(get("deepl_api_url", "") or ""), **common)
        return OpenAITranslator(str(get("openai_base_url", "") or ""),
                                str(get("openai_api_key", "") or ""),
                                str(get("openai_model", "") or ""), **common)

    primary = make(name)
    backups = [p for p in parse_fallback(str(get("translate_fallback", "") or "")) if p != name]
    if not backups:
        return primary
    entries: list[tuple[str, Any]] = [(name, primary)]
    for backup in backups:
        entries.append((backup, make(backup)))
    return ChainTranslator(entries, primary=name)  # type: ignore[return-value]


def translator_cache_key(provider: str, settings: Any) -> tuple:
    """决定是否需要重建引擎：任何影响请求的参数变了都得重建。"""
    def get(name: str, default: Any = "") -> Any:
        if isinstance(settings, dict):
            return settings.get(name, default)
        return getattr(settings, name, default)

    name = normalize_provider(provider)
    base = (name, get("source_language"), get("target_language"),
            get("translate_429_backoff"),
            # 备用渠道变了也要重建，否则新加的备用渠道不会生效
            get("translate_fallback"))
    # 降级链可能用到任何一个渠道的凭据，所以全部纳入缓存键
    return base + (get("openai_base_url"), get("openai_api_key"), get("openai_model"),
                   get("google_api_key"), get("deepl_api_key"), get("deepl_api_url"))
