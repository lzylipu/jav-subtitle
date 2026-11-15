"""刮削：解析、匹配、广告剔除、片长校验、短路顺序。

这里全部用固定 HTML 片段，不打网络——线上站点改版时这些测试仍能跑，
真正的连通性由 /api/scrape/test 在设置页验。
"""
import unittest

from tests import env  # noqa: F401  必须在导入 app 之前设置 DATA_DIR

from app import scraping
from app.subtitles import Cue


SEARCH_HTML = """
<table><tr><td colspan="2">
<a href="subs/1777/PPPD-424_stitched.original.html">PPPD-424 stitched original</a></td></tr>
<tr><td colspan="2"><a href="subs/1322/PPPD-424_MEIN%20WHISPERJAV2.html">PPPD-424 MEIN</a></td></tr>
<tr><td colspan="2"><a href="subs/482/MIDE-286.zh.html">MIDE-286 zh</a></td></tr>
"""

# 详情页里每个语言一条直链，路径里带空格和 × 这种字符
DETAIL_HTML = """
<a href="/subs/1439/MIMK-045 Busty ~ JULIA × Stop-ar.srt">ar</a>
<a href="/subs/713/MIMK-045 Busty ~ JULIA × Stop-zh-CN.srt">zh-CN</a>
<a href="/subs/455/MIMK-045 Busty ~ JULIA × Stop-zh-TW.srt">zh-TW</a>
<a href="/subs/564/MIMK-045 Busty ~ JULIA × Stop-ja.srt">ja</a>
"""

SRT_WITH_ADS = """1
00:00:01,000 --> 00:00:03,000
欢迎加入外挂字幕AA收款群667482011

2
00:00:04,000 --> 00:00:06,000
你回来了

3
00:00:07,000 --> 00:00:09,000
澳門頭號種子賭場就在這裡。

4
00:00:10,000 --> 00:00:12,000
Wzhp1113制作

5
00:00:13,000 --> 00:00:15,000
今天很热
"""


class BuildQueryTests(unittest.TestCase):
    def test_release_code_extracted(self):
        query = scraping.build_query("PPPD-424 JULIA uncensored.mp4")
        self.assertEqual(query.release, "PPPD-424")

    def test_title_strips_noise(self):
        query = scraping.build_query("PPPD-424 [1080p] JULIA.mp4")
        self.assertEqual(query.release, "PPPD-424")
        self.assertNotIn("1080p", query.title.lower())

    def test_query_string_prefers_release(self):
        self.assertEqual(scraping.build_query("ssis-698 cd1.mkv").as_query_string(), "SSIS-698")


class ScoreTests(unittest.TestCase):
    def test_exact_code_scores_full(self):
        query = scraping.build_query("PPPD-424.mp4")
        self.assertEqual(scraping.score_candidate(query, "PPPD-424.V2"), 1.0)

    def test_code_without_dash_still_matches(self):
        query = scraping.build_query("PPPD-424.mp4")
        self.assertGreaterEqual(scraping.score_candidate(query, "pppd424 whisperjav"), 1.0)

    def test_different_code_is_rejected(self):
        query = scraping.build_query("ABP-123.mp4")
        self.assertLess(scraping.score_candidate(query, "ABP-041 MOTTO ENJOY"), scraping.ACCEPT_SCORE)

    def test_title_token_coverage_without_code(self):
        query = scraping.build_query("3-gatsu no Lion")
        self.assertGreaterEqual(scraping.score_candidate(query, "Sangatsu no Lion S1"), 0.0)


class SubtitleCatParsingTests(unittest.TestCase):
    """详情页 → 语言直链。这里最容易出错的是 URL 二次编码。"""

    def setUp(self):
        self.provider = scraping.SubtitleCat(type("S", (), {"scrape_prefer": "zh"})())

    def test_search_extracts_candidates(self):
        found = []
        for match in __import__("re").finditer(r'href="(subs/\d+/[^"]+\.html)"', SEARCH_HTML):
            found.append(match.group(1))
        self.assertEqual(len(found), 3)
        self.assertIn("subs/1322/PPPD-424_MEIN%20WHISPERJAV2.html", found)

    def test_zh_cn_wins_when_preferring_chinese(self):
        code, url = self.provider._pick_language(DETAIL_HTML)
        self.assertEqual(code, "zh-CN")
        self.assertTrue(url.endswith("-zh-CN.srt"))
        # 已有 %20 的路径再编码一次会变成 %2520，站点直接 404
        self.assertNotIn("%2520", url)
        self.assertIn("%20", url)

    def test_ja_wins_when_preferring_japanese(self):
        provider = scraping.SubtitleCat(type("S", (), {"scrape_prefer": "ja"})())
        code, url = provider._pick_language(DETAIL_HTML)
        self.assertEqual(code, "ja")

    def test_no_chinese_falls_back_to_empty(self):
        html = '<a href="/subs/1/x-en.srt">en</a>'
        code, url = self.provider._pick_language(html)
        self.assertEqual((code, url), ("", ""))

    def test_language_of_maps_code(self):
        candidate = scraping.Candidate("subtitlecat", "x", "", "u")
        candidate.extra["lang_code"] = "zh-TW"
        self.assertEqual(self.provider.language_of(candidate), "zh")
        candidate.extra["lang_code"] = "ja"
        self.assertEqual(self.provider.language_of(candidate), "ja")


class AdFilterTests(unittest.TestCase):
    def test_ads_and_credits_removed_dialogue_kept(self):
        cues = scraping.filter_cues([
            Cue(1, "00:00:01,000", "00:00:03,000", "欢迎加入外挂字幕AA收款群667482011"),
            Cue(2, "00:00:04,000", "00:00:06,000", "你回来了"),
            Cue(3, "00:00:07,000", "00:00:09,000", "澳門頭號種子賭場就在這裡。"),
            Cue(4, "00:00:10,000", "00:00:12,000", "Wzhp1113制作"),
            Cue(5, "00:00:13,000", "00:00:15,000", "今天很热"),
        ])
        texts = [c.text for c in cues]
        self.assertEqual(texts, ["你回来了", "今天很热"])
        # 重新编号必须连续，否则播放器会把字幕排乱
        self.assertEqual([c.number for c in cues], [1, 2])

    def test_multiline_cue_keeps_surviving_line(self):
        cues = scraping.filter_cues([Cue(1, "00:00:01,000", "00:00:02,000",
                                         "正常台词\nhttp://spam.example")])
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "正常台词")

    def test_parsing_then_filtering_never_breaks_file(self):
        """广告行是某条字幕的唯一内容时，先删文本再解析会留下空块导致整份失败。"""
        from app.subtitles import parse_srt
        cues = scraping.filter_cues(parse_srt(SRT_WITH_ADS))
        self.assertGreaterEqual(len(cues), 2)
        for cue in cues:
            self.assertTrue(cue.text.strip())


class PlausibilityTests(unittest.TestCase):
    def test_short_subtitle_for_long_video_is_rejected(self):
        cues = [Cue(1, "00:00:01,000", "00:05:00,000", "x")]
        ok, why = scraping.plausibility(7200.0, cues)
        self.assertFalse(ok)
        self.assertIn("分钟", why)

    def test_subtitle_longer_than_video_is_rejected(self):
        cues = [Cue(1, "00:00:01,000", "03:00:00,000", "x")]
        ok, _ = scraping.plausibility(3600.0, cues)
        self.assertFalse(ok)

    def test_matching_length_passes(self):
        cues = [Cue(1, "00:00:01,000", "01:58:00,000", "x")]
        ok, _ = scraping.plausibility(7200.0, cues)
        self.assertTrue(ok)

    def test_unknown_duration_skips_check(self):
        self.assertTrue(scraping.plausibility(0.0, [Cue(1, "0", "1", "x")])[0])


# 刮削结果至少要有 MIN_CUES 条才会被采用，短路测试用份够长的
LONG_SRT = "".join(
    f"{i}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\n第 {i} 句\n\n"
    for i in range(1, 21))


class OrderTests(unittest.TestCase):
    def test_parse_order_dedupes_and_drops_unknown(self):
        order = scraping.parse_order("subtitlecat, kitsunekko ,，subtitlecat,nyaa,addic7ed")
        self.assertEqual(order, ["subtitlecat", "kitsunekko"])

    def test_default_order_skips_key_required(self):
        order = scraping.default_order()
        self.assertIn("subtitlecat", order)
        self.assertNotIn("subdl", order)

    def test_short_circuit_stops_after_first_hit(self):
        """第一个平台命中后，后面的平台不能再被请求。"""
        calls = []

        class FakeCat(scraping.Provider):
            name = "subtitlecat"

            def search(self, query):
                calls.append("subtitlecat")
                return [scraping.Candidate("subtitlecat", "PPPD-424", "", "u",
                                           score=1.0, extra={"lang_code": "zh-CN"})]

            def fetch(self, candidate):
                return LONG_SRT.encode()

            def language_of(self, candidate):
                return "zh"

        class FakeKit(scraping.Provider):
            name = "kitsunekko"

            def search(self, query):
                calls.append("kitsunekko")
                raise AssertionError("短路失效：主平台命中后还在搜备用平台")

        original = dict(scraping.PROVIDER_CLASSES)
        scraping.PROVIDER_CLASSES.update({"subtitlecat": FakeCat, "kitsunekko": FakeKit})
        try:
            settings = type("S", (), {"scrape_enabled": True,
                                      "scrape_providers": "subtitlecat,kitsunekko",
                                      "scrape_prefer": "zh",
                                      "subdl_api_key": "", "opensubtitles_api_key": ""})()
            result = scraping.scrape("PPPD-424.mp4", settings, 0.0)  # 片长未知，跳过时长校验
        finally:
            scraping.PROVIDER_CLASSES.update(original)

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "subtitlecat")
        self.assertEqual(calls, ["subtitlecat"])

    def test_disabled_returns_not_ok(self):
        settings = type("S", (), {"scrape_enabled": False, "scrape_providers": "",
                                 "scrape_prefer": "zh"})()
        self.assertFalse(scraping.scrape("x.mp4", settings).ok)


class DecodeTests(unittest.TestCase):
    def test_shift_jis_decoded(self):
        payload = "おはようございます。".encode("cp932")
        self.assertIn("おはよう", scraping.decode_subtitle(payload))

    def test_ass_converted_to_srt(self):
        ass = ("[Script Info]\nTitle: x\n\n[Events]\n"
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\an8}你好\\N世界\n")
        srt = scraping.ass_to_srt(ass)
        self.assertIn("00:00:01,000 --> 00:00:03,500", srt)
        self.assertIn("你好\n世界", srt)

    def test_vtt_converted_to_srt(self):
        vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\n嗨\n"
        srt = scraping.extract_subtitle_text(vtt.encode(), "a.vtt")
        self.assertIn("00:00:01,000 --> 00:00:02,500", srt)


if __name__ == "__main__":
    unittest.main()
