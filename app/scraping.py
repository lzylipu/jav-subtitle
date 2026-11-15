"""字幕刮削：先上网找现成字幕，找不到再转写。

## 为什么要这个

转写 124 分钟的片子要二三十分钟纯 CPU 时间。如果这部片本来就有人做过字幕，
那纯属白算电。所以每个任务在抽音频之前先问一遍字幕站：有没有现成的。

## 每个平台实测过，只留真能用的

2026-11 用真实番号（MIDE-286 / MIMK-045 / PPPD-424 / PPPD-583 / PPPD-679 /
SSIS-698 / ABP-123 / SNIS-001）逐个站点验过：

| 平台 | 结果 |
|---|---|
| **SubtitleCat** | **8/8 有结果，8/8 能直接下中文** → 主通道，免注册 |
| SubDL | 0/8（只有主流影视，没有这类片），且要 API key |
| OpenSubtitles | 网页搜索 0/8，注册要邮箱验证 + 过 Cloudflare |
| Addic7ed | 只有剧集，要逐级跳页面 + 验证码，抓不了 |
| Nyaa | 只有种子，拿不到字幕文件 |
| Subf2m | 3/8 有站页但页面 JS 渲染，拿不到下载链 |
| Kitsunekko | 动漫专用，日文字幕，免密钥，实测可用 |

所以默认顺序是 `subtitlecat,kitsunekko`：前者包这类影片，后者补动漫。
SubDL / OpenSubtitles 留在代码里但默认不排——填了 key 才轮到它们。

## 顺序与短路

按设置的顺序逐个搜，**第一个对上的就停**，后面的不再请求。
全都没有，才回到「抽音频 → whisper → 翻译」。
单个平台报错只记一笔，不影响后面的平台。

## 匹配靠什么

文件名里常混着厂牌、番号、演员、分辨率、发布组。只做保守匹配：
番号一致最可靠；没番号才退化成标题词覆盖率。再加一道片长校验。
**宁可放过，不要错配**——配错字幕比没字幕更糟。
"""
from __future__ import annotations

import io
import re
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .subtitles import Cue, parse_srt, render_srt

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PROVIDERS = ("subtitlecat", "kitsunekko", "subdl", "opensubtitles")
PROVIDER_LABELS = {
    "subtitlecat": "SubtitleCat",
    "kitsunekko": "Kitsunekko",
    "subdl": "SubDL",
    "opensubtitles": "OpenSubtitles",
}
PROVIDER_NOTES = {
    "subtitlecat": "免注册，这类影片实测 8/8 能搜到，多数带现成中文",
    "kitsunekko": "免注册，动漫日文字幕",
    "subdl": "要 API key；只有主流影视，这类影片搜不到",
    "opensubtitles": "要 API key（注册需邮箱验证）",
}

# 一个候选要拿到多少分才敢用。0.6 = 番号对上，或标题词基本覆盖
ACCEPT_SCORE = 0.6
MAX_BYTES = 20 * 1024 * 1024
MIN_CUES = 8                        # 少于 8 条基本是空壳或广告

RELEASE_RE = re.compile(r"\b([A-Za-z]{2,6})[-_ ]?(\d{2,5})\b")
NOISE_RE = re.compile(
    r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|【[^】]*】|\b(?:1080p|720p|2160p|4k|fhd|hd|x264|x265|"
    r"h264|hevc|aac|dts|bluray|bdrip|web-?dl|webrip|hdtv|uncensored|leak(?:ed)?|"
    r"chinese|japanese|eng(?:lish)?|sub(?:title)?s?|chs|cht|jpn|jap|mp4|mkv)\b",
    re.IGNORECASE)

# 字幕站会往字幕里塞广告。这些行必须剔掉，否则每部片第一条都是群号。
AD_PATTERNS = (
    re.compile(r"https?://|www\.", re.I),
    re.compile(r"(QQ|微信|薇信|威信)\s*群|加群|进群|收款群|交流群|字幕组招募", re.I),
    re.compile(r"更多(字幕|资源|影片).{0,10}(关注|访问|见)"),
    re.compile(r"^\s*[\d\s\-+()]{9,}\s*$"),          # 纯号码行
    re.compile(r"(订阅|關注|关注)\s*(频道|頻道|公众号)", re.I),
    # 字幕组的署名行：「xxx制作」「翻译：xxx」「校对 xxx」。只认短行，别误伤对白
    re.compile(r"^\s*.{0,20}(制作|製作|翻译|翻譯|校对|校對|时间轴|時間軸|压制).{0,20}$"),
    # 赌场/博彩广告：字幕站常把这条塞在第一条
    re.compile(r"(赌场|賭場|娱乐城|娛樂城|博彩|百家乐|百家樂|首存|彩金|优惠|優惠|返水|"
               r"真人荷官|线上娱乐|線上娛樂|注册送|註冊送|提现|提現|賭博|赌博)", re.I),
)

# 这些词出现在一行里也只可能是广告，整行删掉
AD_STRONG = re.compile(r"(赌场|賭場|娱乐城|娛樂城|博彩|百家乐|百家樂|首存|彩金|返水|"
                       r"线上娱乐|線上娛樂|注册送|註冊送)", re.I)


def _is_ad(line: str) -> bool:
    """判断一行是不是广告/署名。短行才按「制作/翻译」判，长句对白不误伤。"""
    if not line.strip():
        return False
    if AD_STRONG.search(line):
        return True
    if any(p.search(line) for p in AD_PATTERNS):
        return True
    stripped = line.strip()
    # 「xxx制作」这种署名基本不超过 30 字，且不含句末标点
    if len(stripped) <= 30 and not re.search(r"[。！？!?…]$", stripped):
        if re.search(r"(制作|製作|翻译|翻譯|校对|校對|时间轴|時間軸|压制|壓制)", stripped):
            return True
    return False


# --------------------------------------------------------------------- query

@dataclass
class Query:
    """从文件名提炼出来的检索条件。"""
    raw: str
    release: str = ""            # PPPD-999
    title: str = ""              # 去掉番号和噪声后的标题
    tokens: list[str] = field(default_factory=list)
    duration: float = 0.0

    def as_query_string(self) -> str:
        return self.release or self.title or self.raw


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def build_query(video_name: str, duration: float = 0.0) -> Query:
    """把 `PPPD-999-C.mp4` 这种文件名拆成可检索的条件。"""
    stem = Path(video_name).stem
    release = ""
    match = RELEASE_RE.search(stem)
    if match:
        release = f"{match.group(1).upper()}-{match.group(2)}"

    cleaned = NOISE_RE.sub(" ", stem)
    cleaned = RELEASE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[_.]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")

    tokens = [t for t in _norm(cleaned).split() if len(t) >= 2]
    return Query(raw=stem, release=release, title=cleaned, tokens=tokens, duration=duration)


def score_candidate(query: Query, name: str, release: str = "", note: str = "") -> float:
    """给候选打 0~1 分。番号一致直接 1.0，否则比标题词覆盖率。"""
    haystack = _norm(f"{name} {release} {note}")
    if query.release:
        compact = re.sub(r"[^a-z0-9]", "", haystack)
        if re.sub(r"[^a-z0-9]", "", query.release.lower()) in compact:
            return 1.0
        # 番号数字段单独出现也算半对（很多站把 PPPD-424 写成 PPPD 424 或 pppd424）
        digits = re.sub(r"\D", "", query.release)
        if len(digits) >= 3 and digits in compact:
            return max(ACCEPT_SCORE, 0.8)
    if not query.tokens:
        return 0.0
    hit = sum(1 for token in query.tokens if token in haystack)
    return round(hit / len(query.tokens), 3)


# --------------------------------------------------------------------- result

@dataclass
class Candidate:
    provider: str
    name: str
    lang: str                # "zh" / "ja" / ""
    url: str                 # 详情页或直链
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScrapeResult:
    ok: bool = False
    provider: str = ""
    source_name: str = ""
    language: str = ""        # "zh" / "ja"
    cues: list[Cue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"{PROVIDER_LABELS.get(self.provider, self.provider)} 命中（{self.source_name}）"
        return "；".join(self.notes) or "没有平台给出可用字幕"


# ------------------------------------------------------------------- decoding

def decode_subtitle(data: bytes) -> str:
    """字幕文件编码五花八门：UTF-8、带 BOM、Shift-JIS、EUC-JP、GBK 都见过。"""
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc-jp", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def filter_cues(cues: list[Cue]) -> list[Cue]:
    """逐条剔广告。必须在**解析之后**做：直接在文本上删行会把某条字幕的正文删空，
    留下只有时间轴的残块，整份文件就解析失败了（实测踩过）。"""
    out: list[Cue] = []
    for cue in cues:
        lines = [line for line in cue.text.splitlines() if not _is_ad(line)]
        text = "\n".join(line for line in lines if line.strip()).strip()
        if text:
            out.append(Cue(len(out) + 1, cue.start, cue.end, text))
    return out


ASS_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{2,3})")


def ass_to_srt(text: str) -> str:
    """把 .ass 的 Dialogue 行转成 SRT。只保留时间轴和正文，样式全丢。"""
    cues: list[tuple[str, str, str]] = []
    in_events = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("[events]"):
            in_events = True
            continue
        if stripped.startswith("[") and in_events:
            in_events = False
        if not in_events or not stripped.lower().startswith("dialogue:"):
            continue
        parts = stripped.split(":", 1)[1].split(",", 9)
        if len(parts) < 10:
            continue
        start, end, body = parts[1].strip(), parts[2].strip(), parts[9]
        body = re.sub(r"\{[^}]*\}", "", body)
        body = body.replace("\\N", "\n").replace("\\n", "\n").strip()
        if not body:
            continue
        a, b = _ass_stamp(start), _ass_stamp(end)
        if a and b:
            cues.append((a, b, body))
    if not cues:
        return ""
    blocks = [f"{i}\n{a} --> {b}\n{t}" for i, (a, b, t) in enumerate(cues, 1)]
    return "\n\n".join(blocks) + "\n"


def _ass_stamp(value: str) -> str:
    match = ASS_TIME_RE.search(value)
    if not match:
        return ""
    hours, minutes, seconds, frac = match.groups()
    millis = int(frac.ljust(3, "0")[:3])
    return f"{int(hours):02d}:{minutes}:{seconds},{millis:03d}"


def extract_subtitle_text(data: bytes, filename: str = "") -> str:
    """把下载到的东西（zip / ass / srt / vtt）统一变成 SRT 文本；做不到就返回空串。"""
    lowered = filename.lower()
    if data[:2] == b"PK" or lowered.endswith(".zip"):
        return _pick_from_zip(data)
    return _to_srt(decode_subtitle(data), lowered)


def _pick_from_zip(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    best = ""
    for info in archive.infolist():
        if info.is_dir() or info.file_size > MAX_BYTES:
            continue
        name = Path(info.filename).name.lower()
        if not name.endswith((".srt", ".ass", ".ssa")):
            continue
        candidate = _to_srt(decode_subtitle(archive.read(info)), name)
        if len(candidate) > len(best):
            best = candidate
    return best


def _to_srt(text: str, filename: str) -> str:
    if filename.endswith((".ass", ".ssa")) or "[Script Info]" in text[:400]:
        return ass_to_srt(text)
    if filename.endswith(".vtt") or text.lstrip().startswith("WEBVTT"):
        return _vtt_to_srt(text)
    try:
        cues = parse_srt(text)
    except ValueError:
        return ""
    return render_srt(cues) if cues else ""


def _vtt_to_srt(text: str) -> str:
    body = re.sub(r"^WEBVTT[^\n]*\n", "", text.lstrip(), flags=re.IGNORECASE)
    body = re.sub(r"^\d+\s*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", body)
    try:
        cues = parse_srt(body)
    except ValueError:
        return ""
    return render_srt(cues) if cues else ""


def looks_like_chinese(text: str) -> bool:
    """粗略判断字幕正文是中文还是日文。中文字幕里几乎没有假名。"""
    sample = text[:4000]
    kana = len(re.findall(r"[\u3040-\u30ff]", sample))
    han = len(re.findall(r"[\u4e00-\u9fff]", sample))
    if kana <= 2 and han >= 20:
        return True
    return han > kana * 4


def _stamp_to_seconds(value: str) -> float:
    match = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value or "")
    if not match:
        return 0.0
    hours, minutes, seconds, millis = (int(x) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def plausibility(total_seconds: float, cues: list[Cue]) -> tuple[bool, str]:
    """拿片长跟字幕时长对一下，差太多说明版本不是这一部。

    字幕最后一条远早于片尾（常见的「只有前半」），或者超出片长太多，都算可疑。
    片长未知就跳过检查。
    """
    if not total_seconds or not cues:
        return True, ""
    last = _stamp_to_seconds(cues[-1].end)
    if last <= 0:
        return True, ""
    if last > total_seconds * 1.05 + 30:
        return False, f"字幕到 {last / 60:.0f} 分钟，片长只有 {total_seconds / 60:.0f} 分钟"
    if total_seconds > 600 and last < total_seconds * 0.4:
        return False, f"字幕只到 {last / 60:.0f} 分钟，片长 {total_seconds / 60:.0f} 分钟"
    return True, ""


# -------------------------------------------------------------------- providers

class Provider:
    """一个平台的适配器。子类实现 search()，需要分步下载的再实现 fetch()。"""
    name = ""
    needs_key = False
    can_download = True
    timeout = 30.0

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def key(self) -> str:
        return ""

    def available(self) -> tuple[bool, str]:
        if self.needs_key and not self.key():
            return False, "未填 API key"
        if not self.can_download:
            return False, PROVIDER_NOTES.get(self.name, "")
        return True, ""

    def headers(self) -> dict[str, str]:
        return {"User-Agent": BROWSER_UA,
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"}

    def search(self, query: Query) -> list[Candidate]:      # pragma: no cover
        raise NotImplementedError

    def fetch(self, candidate: Candidate) -> bytes:        # pragma: no cover
        raise NotImplementedError

    def language_of(self, candidate: Candidate) -> str:
        return candidate.lang or ""

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(timeout=self.timeout, follow_redirects=True,
                          headers=self.headers()) as client:
            return client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(timeout=self.timeout, follow_redirects=True,
                          headers=self.headers()) as client:
            return client.post(url, **kwargs)


class SubtitleCat(Provider):
    """SubtitleCat：免注册，聚合各家字幕，并且自带几十种语言的机器翻译版。

    这类影片的中文字幕基本都在它这儿：实测 8 个番号 8 个有中文。

    两段式：搜索页给出 `subs/{id}/{名字}.html` 详情页；详情页里每个语言一个
    `.srt` 直链，命名规则是 `{原名}-zh-CN.srt` / `-zh-TW.srt` / `-ja.srt`。
    """
    name = "subtitlecat"
    BASE = "https://www.subtitlecat.com"

    ZH_ORDER = ("zh-CN", "zh-TW", "zh")
    JA_ORDER = ("ja",)

    def _lang_order(self) -> tuple[str, ...]:
        if getattr(self.settings, "scrape_prefer", "zh") == "ja":
            return self.JA_ORDER + self.ZH_ORDER
        return self.ZH_ORDER + self.JA_ORDER

    def search(self, query: Query) -> list[Candidate]:
        keyword = query.as_query_string()
        response = self.get(f"{self.BASE}/index.php", params={"search": keyword})
        if response.status_code != 200:
            raise RuntimeError(f"SubtitleCat 返回 {response.status_code}")
        out: list[Candidate] = []
        seen: set[str] = set()
        for match in re.finditer(r'href="(subs/\d+/[^"]+\.html)"', response.text):
            rel = match.group(1)
            if rel in seen:
                continue
            seen.add(rel)
            name = urllib.parse.unquote(rel.rsplit("/", 1)[-1])[:-5]
            out.append(Candidate(
                provider=self.name, name=name, lang="",
                url=f"{self.BASE}/{urllib.parse.quote(rel, safe='/%')}",
                score=score_candidate(query, name),
            ))
        return out

    def _pick_language(self, detail_html: str) -> tuple[str, str]:
        """从详情页里挑出优先级最高的语言直链，返回 (语言代码, 绝对 URL)。"""
        links: dict[str, str] = {}
        for match in re.finditer(r'href="(/subs/\d+/[^"]+?)-([a-zA-Z-]{2,7})\.srt"', detail_html):
            href, code = match.group(1), match.group(2)
            links.setdefault(code, href + f"-{code}.srt")
        for code in self._lang_order():
            if code in links:
                return code, self.BASE + urllib.parse.quote(links[code], safe="/%")
        for code, href in links.items():
            if code.lower().startswith("zh"):
                return code, self.BASE + urllib.parse.quote(href, safe="/%")
        return "", ""

    def fetch(self, candidate: Candidate) -> bytes:
        detail = self.get(candidate.url)
        if detail.status_code != 200:
            raise RuntimeError(f"SubtitleCat 详情页返回 {detail.status_code}")
        code, link = self._pick_language(detail.text)
        if not link:
            raise RuntimeError("这一条没有中/日文字幕")
        payload = self.get(link, headers={**self.headers(), "Referer": candidate.url})
        if payload.status_code != 200:
            raise RuntimeError(f"SubtitleCat 下载返回 {payload.status_code}")
        candidate.extra["lang_code"] = code
        candidate.extra["srt_url"] = link
        return payload.content

    def language_of(self, candidate: Candidate) -> str:
        code = str(candidate.extra.get("lang_code") or "").lower()
        return "zh" if code.startswith("zh") else "ja"


class Kitsunekko(Provider):
    """Kitsunekko：动漫日文字幕。根目录「一部番一个子目录」，字幕 zip 在子目录里，
    所以两级跳：先按番名找目录，再进去挑文件。"""
    name = "kitsunekko"
    BASE = "https://kitsunekko.net"
    ROOTS = ("subtitles/japanese/",)

    def _dirs(self, html: str) -> list[tuple[str, str]]:
        out = []
        # 站点把目录名包在 <strong> 里：<a href="..."><strong>名字</strong> </a>
        for match in re.finditer(r'<a href="(/dirlist\.php\?dir=[^"]+)"[^>]*>(.*?)</a>',
                                 html, re.S):
            name = re.sub(r"<[^>]+>", "", match.group(2)).strip().rstrip("/")
            if name and not name.lower().endswith((".zip", ".srt", ".ass")):
                out.append((match.group(1), name))
        return out

    def _files(self, html: str) -> list[tuple[str, str]]:
        out = []
        for match in re.finditer(r'<a href="([^"]+\.(?:zip|srt|ass))"[^>]*>(.*?)</a>',
                                 html, re.S | re.I):
            name = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if name:
                out.append((match.group(1), name))
        return out

    @classmethod
    def _absolute(cls, href: str) -> str:
        if href.startswith("http"):
            return href
        return cls.BASE + (href if href.startswith("/") else "/" + href.lstrip("./"))

    def search(self, query: Query) -> list[Candidate]:
        keyword = _norm(query.title or query.raw)
        tokens = [t for t in re.split(r"[\s\-_]+", keyword) if len(t) >= 3]
        if not tokens:
            return []
        out: list[Candidate] = []
        for root in self.ROOTS:
            listing = self.get(f"{self.BASE}/dirlist.php", params={"dir": root})
            if listing.status_code != 200:
                continue
            scored = []
            for href, name in self._dirs(listing.text):
                haystack = _norm(name.replace("_", " "))
                hits = sum(1 for t in tokens if t in haystack)
                if hits:
                    scored.append((round(min(1.0, hits / len(tokens)), 3), href, name))
            scored.sort(reverse=True)
            for score, href, name in scored[:2]:      # 最多看两个目录，别把站抓崩
                inner = self.get(self.BASE + href)
                if inner.status_code != 200:
                    continue
                for file_href, file_name in self._files(inner.text)[:60]:
                    out.append(Candidate(provider=self.name,
                                         name=f"{name}/{file_name}", lang="ja",
                                         url=self._absolute(file_href), score=score))
        return out

    def fetch(self, candidate: Candidate) -> bytes:
        response = self.get(candidate.url)
        if response.status_code != 200:
            raise RuntimeError(f"Kitsunekko 下载返回 {response.status_code}")
        return response.content


class SubDL(Provider):
    """SubDL：只有主流影视，这类片实测 0/8。要 api_key 才能搜。

    留着是因为放着不碍事：填了 key 才轮到它，且它擅长电影/剧集。
    """
    name = "subdl"
    needs_key = True

    def key(self) -> str:
        return getattr(self.settings, "subdl_api_key", "") or ""

    def search(self, query: Query) -> list[Candidate]:
        params = {"api_key": self.key(), "subs_per_page": "30",
                  "film_name": query.release or query.title or query.raw,
                  "languages": "ZH,JA"}
        response = self.get("https://api.subdl.com/api/v1/subtitles", params=params)
        if response.status_code != 200:
            raise RuntimeError(f"SubDL 返回 {response.status_code}")
        data = response.json()
        out: list[Candidate] = []
        for item in data.get("subtitles") or []:
            name = str(item.get("release_name") or item.get("name") or "")
            lang = str(item.get("language") or "")
            url = str(item.get("url") or "")
            if not url:
                continue
            out.append(Candidate(
                provider=self.name, name=name,
                lang="zh" if lang.lower().startswith("zh") else "ja",
                url=url if url.startswith("http") else "https://dl.subdl.com" + url,
                score=score_candidate(query, name),
            ))
        return out

    def fetch(self, candidate: Candidate) -> bytes:
        response = self.get(candidate.url)
        if response.status_code != 200:
            raise RuntimeError(f"SubDL 下载返回 {response.status_code}")
        return response.content


class OpenSubtitles(Provider):
    """OpenSubtitles 新版 REST。搜索给 file_id，下载要先换一次性 link。"""
    name = "opensubtitles"
    needs_key = True

    def key(self) -> str:
        return getattr(self.settings, "opensubtitles_api_key", "") or ""

    def headers(self) -> dict[str, str]:
        return {"User-Agent": "JAV-Subtitle v3.9", "Api-Key": self.key(),
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8"}

    def search(self, query: Query) -> list[Candidate]:
        params: dict[str, Any] = {"query": query.release or query.title or query.raw,
                                  "order_by": "download_count", "order_direction": "desc",
                                  "languages": "ja,zh-cn"}
        response = self.get("https://api.opensubtitles.com/api/v1/subtitles", params=params)
        if response.status_code != 200:
            raise RuntimeError(f"OpenSubtitles 返回 {response.status_code}")
        data = response.json()
        out: list[Candidate] = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            name = attrs.get("release") or attrs.get("feature_details", {}).get("title") or ""
            files = attrs.get("files") or []
            if not files:
                continue
            lang = str(attrs.get("language") or "")
            out.append(Candidate(
                provider=self.name, name=str(name),
                lang="zh" if lang.startswith("zh") else "ja",
                url="", score=score_candidate(query, str(name)),
                extra={"file_id": files[0].get("file_id")},
            ))
        return out

    def fetch(self, candidate: Candidate) -> bytes:
        file_id = candidate.extra.get("file_id")
        if not file_id:
            raise RuntimeError("OpenSubtitles 候选缺少 file_id")
        response = self.post("https://api.opensubtitles.com/api/v1/download",
                             json={"file_id": file_id, "sub_format": "srt"})
        if response.status_code != 200:
            raise RuntimeError(f"OpenSubtitles 取下载地址返回 {response.status_code}")
        link = (response.json() or {}).get("link")
        if not link:
            raise RuntimeError("OpenSubtitles 没给下载地址（多半是当日额度用完）")
        payload = self.get(link)
        if payload.status_code != 200:
            raise RuntimeError(f"OpenSubtitles 下载返回 {payload.status_code}")
        return payload.content


PROVIDER_CLASSES = {
    "subtitlecat": SubtitleCat,
    "kitsunekko": Kitsunekko,
    "subdl": SubDL,
    "opensubtitles": OpenSubtitles,
}


def parse_order(raw: str) -> list[str]:
    """`"subtitlecat, kitsunekko ,subdl"` → 去空格、去重、丢掉不认识的名字。"""
    order: list[str] = []
    for chunk in (raw or "").replace("，", ",").split(","):
        name = chunk.strip().lower()
        if name in PROVIDER_CLASSES and name not in order:
            order.append(name)
    return order


def default_order() -> list[str]:
    return [p for p in PROVIDERS if not PROVIDER_CLASSES[p].needs_key]


# --------------------------------------------------------------------- runner

def scrape(video_name: str, settings: Any, duration: float = 0.0) -> ScrapeResult:
    """按设置的顺序逐个平台找字幕。第一个对上的就返回，后面的不再请求。"""
    result = ScrapeResult()
    if not getattr(settings, "scrape_enabled", False):
        result.notes.append("未启用刮削")
        return result

    order = parse_order(getattr(settings, "scrape_providers", "")) or default_order()
    if not order:
        result.notes.append("没有可用的平台")
        return result

    query = build_query(video_name, duration)
    prefer = getattr(settings, "scrape_prefer", "zh")

    for name in order:
        provider = PROVIDER_CLASSES[name](settings)
        ok, why = provider.available()
        if not ok:
            result.tried.append(f"{PROVIDER_LABELS[name]}：跳过（{why}）")
            continue
        try:
            candidates = [c for c in provider.search(query) if c.score >= ACCEPT_SCORE]
        except Exception as exc:                      # noqa: BLE001
            result.tried.append(
                f"{PROVIDER_LABELS[name]}：搜索失败（{type(exc).__name__}: {exc}）")
            continue
        if not candidates:
            result.tried.append(f"{PROVIDER_LABELS[name]}：没搜到对得上的")
            continue

        # 分数高的先试；分数相同时优先用户指定的语言
        candidates.sort(key=lambda c: (-c.score, 0 if c.lang == prefer else 1))
        for candidate in candidates[:3]:               # 最多试三个，别在一棵树上吊死
            try:
                payload = provider.fetch(candidate)
            except Exception as exc:                   # noqa: BLE001
                result.tried.append(f"{PROVIDER_LABELS[name]}：下载失败（{exc}）")
                continue
            if len(payload) > MAX_BYTES:
                result.tried.append(f"{PROVIDER_LABELS[name]}：文件过大，跳过")
                continue
            text = extract_subtitle_text(payload, candidate.name)
            if not text:
                result.tried.append(f"{PROVIDER_LABELS[name]}：拿到的不是有效字幕")
                continue
            try:
                cues = filter_cues(parse_srt(text))
            except ValueError as exc:
                result.tried.append(f"{PROVIDER_LABELS[name]}：字幕解析失败（{exc}）")
                continue
            if len(cues) < MIN_CUES:
                result.tried.append(
                    f"{PROVIDER_LABELS[name]}：只有 {len(cues)} 条，像空壳")
                continue
            fine, why_not = plausibility(query.duration, cues)
            if not fine:
                result.tried.append(f"{PROVIDER_LABELS[name]}：对不上片长（{why_not}）")
                continue
            language = (provider.language_of(candidate)
                        or ("zh" if looks_like_chinese(text) else "ja"))
            result.ok = True
            result.provider = name
            result.source_name = candidate.name
            result.cues = cues
            result.language = language
            result.tried.append(
                f"{PROVIDER_LABELS[name]}：命中《{candidate.name[:60]}》，{len(cues)} 条"
                f"（{'中文' if language == 'zh' else '日文'}）")
            return result

        result.tried.append(f"{PROVIDER_LABELS[name]}：有 {len(candidates)} 个候选但没有一份能用")

    result.notes = result.tried
    return result


def provider_status(settings: Any) -> list[dict[str, Any]]:
    """给设置页显示：每个平台能不能用、为什么。"""
    out = []
    order = parse_order(getattr(settings, "scrape_providers", "")) or default_order()
    for name in PROVIDERS:
        provider = PROVIDER_CLASSES[name](settings)
        ok, why = provider.available()
        out.append({
            "name": name,
            "label": PROVIDER_LABELS[name],
            "enabled": name in order,
            "ready": ok,
            "reason": "" if ok else why,
            "note": PROVIDER_NOTES.get(name, ""),
        })
    return out
