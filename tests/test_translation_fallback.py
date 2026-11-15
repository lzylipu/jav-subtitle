"""渠道自动降级：主渠道整库不可用时自动切备用渠道。

真实事故：DeepL 的 key 失效，5 个任务全在「翻译第 1 批」就 403 失败，
整库停摆等人来改设置。这里的用例锁住降级行为：
- 只对「整库都跑不动」的错切换（鉴权失败 / 额度用光 / 完全没配置）
- 429 限流、条数不符这类偶发错不切，免得两个渠道来回抖
- 切换后粘住，不要每批都去撞一遍已经死掉的渠道
- 切换只播报一次，别把日志刷满
"""
import unittest

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from app.config import Settings
from app.translation import (ChainTranslator, FatalTranslationError,
                             MissingConfigError, TranslationError,
                             build_translator, parse_fallback)


class FakeEngine:
    """假渠道：按预设的行为返回或抛错，并记录被调用次数。"""

    def __init__(self, name, behavior="ok", text="译文"):
        self.name = name
        self.behavior = behavior
        self.text = text
        self.calls = 0
        self.last_misses = []
        self.closed = False

    def translate_texts(self, texts):
        self.calls += 1
        if self.behavior == "fatal":
            raise FatalTranslationError("HTTP 403 Forbidden")
        if self.behavior == "missing":
            raise MissingConfigError("没填 key")
        if self.behavior == "rate":
            raise TranslationError("上游限流 429")
        return [self.text] * len(texts)

    def key_report(self):
        return f"{self.name} 的密钥形状"

    def usage(self):
        return {"ok": True, "used": 0, "limit": 500000}

    def close(self):
        self.closed = True


class ParseFallbackTests(unittest.TestCase):
    def test_plain_list(self):
        self.assertEqual(parse_fallback("google,bing"), ["google", "bing"])

    def test_spaces_and_fullwidth_comma(self):
        self.assertEqual(parse_fallback(" google ， bing "), ["google", "bing"])

    def test_unknown_names_are_dropped(self):
        self.assertEqual(parse_fallback("google,bogus,bing"), ["google", "bing"])

    def test_empty_means_disabled(self):
        self.assertEqual(parse_fallback(""), [])
        self.assertEqual(parse_fallback(None), [])

    def test_duplicates_collapse(self):
        self.assertEqual(parse_fallback("google,google,bing"), ["google", "bing"])


class ChainTests(unittest.TestCase):
    def chain(self, *behaviors):
        entries = [(name, FakeEngine(name, beh))
                   for name, beh in zip(["deepl", "google", "bing"], behaviors)]
        return ChainTranslator(entries, primary="deepl")

    def test_healthy_primary_is_used_directly(self):
        tr = self.chain("ok", "ok")
        self.assertEqual(tr.translate_texts(["a", "b"]), ["译文", "译文"])
        self.assertFalse(tr.degraded)
        self.assertEqual(tr.active_name, "deepl")
        self.assertEqual(tr.entries[1][1].calls, 0)   # 备用渠道没被碰

    def test_fatal_error_switches_to_backup(self):
        tr = self.chain("fatal", "ok")
        self.assertEqual(tr.translate_texts(["a"]), ["译文"])
        self.assertTrue(tr.degraded)
        self.assertEqual(tr.active_name, "google")
        self.assertEqual(len(tr.switch_notes), 1)
        # 播报用中文渠道名，用户一眼能看懂
        self.assertIn("DeepL", tr.switch_notes[0])
        self.assertIn("谷歌", tr.switch_notes[0])
        self.assertIn("403", tr.switch_notes[0])

    def test_missing_config_also_switches(self):
        tr = self.chain("missing", "ok")
        self.assertEqual(tr.translate_texts(["a"]), ["译文"])
        self.assertTrue(tr.degraded)

    def test_backup_is_sticky_after_switch(self):
        tr = self.chain("fatal", "ok")
        tr.translate_texts(["a"])
        tr.translate_texts(["b"])
        tr.translate_texts(["c"])
        # 死掉的主渠道只撞一次，后面直接走备用
        self.assertEqual(tr.entries[0][1].calls, 1)
        self.assertEqual(tr.entries[1][1].calls, 3)

    def test_switch_note_is_announced_only_once(self):
        tr = self.chain("fatal", "ok")
        for _ in range(5):
            tr.translate_texts(["a"])
        self.assertEqual(len(tr.switch_notes), 1)

    def test_rate_limit_does_not_switch(self):
        """429 由渠道自己退避重试，切渠道只会让两个站同时被撞。"""
        tr = self.chain("rate", "ok")
        with self.assertRaises(TranslationError):
            tr.translate_texts(["a"])
        self.assertEqual(tr.entries[1][1].calls, 0)

    def test_all_dead_raises_with_every_reason(self):
        tr = self.chain("fatal", "fatal", "fatal")
        with self.assertRaises(FatalTranslationError) as ctx:
            tr.translate_texts(["a"])
        text = str(ctx.exception)
        self.assertIn("所有翻译渠道都不可用", text)
        self.assertIn("deepl", text)
        self.assertIn("google", text)
        self.assertIn("bing", text)

    def test_second_hop_when_first_backup_also_dies(self):
        tr = self.chain("fatal", "fatal", "ok")
        self.assertEqual(tr.translate_texts(["a"]), ["译文"])
        self.assertEqual(tr.active_name, "bing")
        self.assertEqual(len(tr.switch_notes), 2)

    def test_empty_text_list_needs_no_call(self):
        tr = self.chain("fatal", "ok")
        self.assertEqual(tr.translate_texts([]), [])
        self.assertEqual(tr.entries[0][1].calls, 0)

    def test_close_closes_every_engine(self):
        tr = self.chain("ok", "ok", "ok")
        tr.close()
        self.assertTrue(all(e.closed for _, e in tr.entries))


class BuildTranslatorTests(unittest.TestCase):
    def test_no_fallback_returns_single_engine(self):
        s = Settings()
        s.translate_provider = "google"
        self.assertNotIsInstance(build_translator("google", s), ChainTranslator)

    def test_fallback_builds_chain(self):
        s = Settings()
        s.translate_provider = "deepl"
        s.translate_fallback = "google,bing"
        tr = build_translator("deepl", s)
        self.assertIsInstance(tr, ChainTranslator)
        self.assertEqual([n for n, _ in tr.entries], ["deepl", "google", "bing"])
        tr.close()

    def test_primary_is_skipped_in_fallback_list(self):
        s = Settings()
        s.translate_provider = "google"
        s.translate_fallback = "google,bing"
        tr = build_translator("google", s)
        self.assertEqual([n for n, _ in tr.entries], ["google", "bing"])
        tr.close()

    def test_chain_reports_active_engine_shape(self):
        s = Settings()
        s.translate_provider = "deepl"
        s.translate_fallback = "google"
        s.deepl_api_key = "00000000-0000-4000-8000-000000000000:fx"
        tr = build_translator("deepl", s)
        self.assertIn("api-free.deepl.com", tr.key_report())
        tr.close()


if __name__ == "__main__":
    unittest.main()
