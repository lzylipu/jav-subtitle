"""DeepL 的 403 自查：报错必须说清「是 key 的问题，不是条数/字数的问题」。

真实事故：用户换了 DeepL 免费 key 仍然 403，怀疑是「一次发太多字」。
实际把一句 20 字的短句发过去同样 403 —— 403 只代表 key 无效或订阅没生效。
所以这里锁死三件事：
1. key 形状（长度 / 有没有 :fx / 打哪个端点）要出现在报错里
2. 报错里必须写明与条数无关
3. 密钥原文一个字都不能出现在报错里
"""
import unittest

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from app.translation import DeepLTranslator, FatalTranslationError, TranslationError


class DeepLKeyReportTests(unittest.TestCase):
    def test_free_key_goes_to_free_endpoint(self):
        t = DeepLTranslator("00000000-0000-4000-8000-000000000000:fx")
        self.assertEqual(t.endpoint, DeepLTranslator.FREE_ENDPOINT)
        self.assertIn(":fx", t.key_report())
        self.assertIn("api-free.deepl.com", t.key_report())

    def test_key_without_fx_goes_to_pro_endpoint_and_says_so(self):
        t = DeepLTranslator("00000000-0000-4000-8000-000000000000")
        self.assertEqual(t.endpoint, DeepLTranslator.PRO_ENDPOINT)
        self.assertIn("不以 :fx 结尾", t.key_report())

    def test_key_report_never_leaks_the_key(self):
        secret = "00000000-0000-4000-8000-000000000000:fx"
        t = DeepLTranslator(secret)
        report = t.key_report()
        self.assertNotIn(secret, report)
        self.assertNotIn("abcd1234", report)

    def test_usage_url_follows_the_endpoint(self):
        t = DeepLTranslator("dummy:fx")
        self.assertEqual(t.endpoint.rsplit("/v2/", 1)[0] + "/v2/usage",
                         "https://api-free.deepl.com/v2/usage")

    def test_base_translator_has_no_key_report(self):
        from app.translation import build_translator
        from app.config import Settings
        engine = build_translator("google", Settings())
        self.assertEqual(engine.key_report(), "")
        self.assertFalse(engine.usage()["ok"])


class DeepL403MessageTests(unittest.TestCase):
    """用假 key 打真端点：403 一定发生，正好验证报错文案。

    这条要真连外网。网络抖一下（SSL EOF / 超时）不该算代码坏了，
    所以连不上就跳过 —— 但一旦连上，403 文案必须完全对。
    """

    def test_403_message_says_it_is_not_about_batch_size(self):
        t = DeepLTranslator("dummy-key-invalid:fx", timeout=30, retries=1,
                            rpm=0, min_interval=0)
        try:
            with self.assertRaises(FatalTranslationError) as ctx:
                t.translate_texts(["こんにちは"])
        except TranslationError as exc:            # 网络层错，不是 403
            self.skipTest(f"连不上 DeepL，跳过这条真网络断言：{exc}")
        finally:
            pass
        text = str(ctx.exception)
        self.assertIn("403", text)
        self.assertIn("自查", text)
        self.assertIn("与一次发多少条", text)
        self.assertNotIn("dummy-key-invalid:fx", text)   # 密钥原文不许出现
        t.close()


if __name__ == "__main__":
    unittest.main()
