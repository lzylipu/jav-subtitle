"""多渠道翻译引擎的约束。

这几条测试要钉死的东西：
1. 用户从网页/聊天粘进来的 key 必须被收拾干净（引号、空格、"Bearer " 前缀
   ——这些正是「key 在别处能用、到项目里 401」最常见的原因）。
2. 空字幕行不发给上游，但回来的译文必须还在原来的位置。
3. 上游返回条数不对必须当错误，不许错位写进字幕。
4. 翻译记忆的命名空间必须按渠道分开，机翻结果不能污染大模型缓存。
5. 谷歌/必应/DeepL 各自发出去的请求形态要对得上（端点、参数、鉴权头）。
"""
import json
import unittest

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR
import httpx

from app import translation as T


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class CleanSecretTests(unittest.TestCase):
    def test_strips_whitespace_and_quotes(self):
        self.assertEqual(T.clean_secret('  "sk-abc123"  '), "sk-abc123")
        self.assertEqual(T.clean_secret("'sk-abc123'"), "sk-abc123")
        self.assertEqual(T.clean_secret("\tsk-abc123\n"), "sk-abc123")

    def test_strips_bearer_prefix(self):
        self.assertEqual(T.clean_secret("Bearer sk-abc123"), "sk-abc123")
        self.assertEqual(T.clean_secret("bearer   sk-abc123"), "sk-abc123")

    def test_strips_deepl_auth_key_prefix(self):
        self.assertEqual(T.clean_secret("DeepL-Auth-Key abc:fx"), "abc:fx")

    def test_empty_and_none(self):
        self.assertEqual(T.clean_secret(""), "")
        self.assertEqual(T.clean_secret(None), "")

    def test_provider_normalisation(self):
        self.assertEqual(T.normalize_provider("Google"), "google")
        self.assertEqual(T.normalize_provider(" DEEPL "), "deepl")
        self.assertEqual(T.normalize_provider("nonsense"), "openai")
        self.assertEqual(T.normalize_provider(None), "openai")

    def test_cache_namespace_is_per_provider(self):
        self.assertEqual(T.cache_namespace("openai", "gpt-4o-mini"), "openai:gpt-4o-mini")
        self.assertEqual(T.cache_namespace("google", "gpt-4o-mini"), "google")
        self.assertEqual(T.cache_namespace("bing", "gpt-4o-mini"), "bing")
        self.assertNotEqual(T.cache_namespace("google"), T.cache_namespace("bing"))


class FakeTranslator(T.BaseTranslator):
    """把上游换成一个可控函数，专测对齐/重试逻辑。"""

    def __init__(self, fn=None, **kwargs):
        super().__init__(**kwargs)
        self.fn = fn or (lambda texts: [t + "·译" for t in texts])
        self.calls = []

    def _request(self, texts):
        self.calls.append(list(texts))
        return self.fn(texts)


class AlignmentTests(unittest.TestCase):
    def test_blank_lines_are_skipped_but_keep_position(self):
        tr = FakeTranslator()
        out = tr.translate_texts(["おはよう", "   ", "ありがとう", ""])
        self.assertEqual(out, ["おはよう·译", "", "ありがとう·译", ""])
        self.assertEqual(tr.calls, [["おはよう", "ありがとう"]])

    def test_all_blank_makes_no_request(self):
        tr = FakeTranslator()
        out = tr.translate_texts(["", "  "])
        self.assertEqual(out, ["", ""])
        self.assertEqual(tr.calls, [])

    def test_short_response_is_recovered_by_single_line_retry(self):
        # 上游整批只回一条时，逐条重发能把对齐救回来（每条回一条就是对的）
        tr = FakeTranslator(fn=lambda texts: ["只有一条"], retries=1)
        self.assertEqual(tr.translate_texts(["a", "b"]), ["只有一条", "只有一条"])

    def test_single_line_short_response_still_errors(self):
        # 单条也回不对就是上游真有问题，必须报错，不许写错位或空白字幕
        tr = FakeTranslator(fn=lambda texts: [], retries=1)
        with self.assertRaises(T.TranslationError):
            tr.translate_texts(["a"])

    def test_one_poison_line_does_not_kill_the_whole_batch(self):
        # 真实场景：一部片几千条，某一批里有一句模型死活翻不对。
        # 老版本会整部片作废，现在只保留那一句日文原文，其余照常译。
        def fn(texts):
            if "毒" in texts:
                raise T.TranslationError("这句翻不过")
            return [t + "·译" for t in texts]
        tr = FakeTranslator(fn=fn, retries=1)
        out = tr.translate_texts(["おはよう", "毒", "ありがとう"])
        self.assertEqual(out, ["おはよう·译", "毒", "ありがとう·译"])
        self.assertEqual(tr.last_misses, ["毒"])

    def test_all_lines_failing_is_still_an_error(self):
        # 整批全废属于上游故障，不能当成功，否则用户拿到全日文的「完成」字幕
        tr = FakeTranslator(fn=lambda texts: [(_ for _ in ()).throw(T.TranslationError("全挂"))],
                            retries=1)
        with self.assertRaises(T.TranslationError):
            tr.translate_texts(["a", "b"])

    def test_salvage_gives_up_after_consecutive_failures(self):
        # 上游整体挂掉时不能一条条耗在限流闸门前几十分钟
        calls = []

        def fn(texts):
            calls.append(len(texts))
            raise T.TranslationError("上游整体故障")
        tr = FakeTranslator(fn=fn, retries=1)
        with self.assertRaises(T.TranslationError):
            tr.translate_texts([f"第{i}句" for i in range(30)] + ["尾巴"])
        # 31 条：二分 2 层（1+2+4）+ 逐条抢救最多 3 条，不该无限往外打
        self.assertLessEqual(len(calls), 15)

    def test_failed_batch_splits_instead_of_dying(self):
        # 上游对两行一起的请求报错，单行则成功——二分拆分要把整批救回来
        def fn(texts):
            if len(texts) > 1:
                raise T.TranslationError("上游炸了")
            return [texts[0] + "·译"]
        tr = FakeTranslator(fn=fn, retries=1)
        self.assertEqual(tr.translate_texts(["a", "b"]), ["a·译", "b·译"])

    def test_fatal_error_is_not_retried(self):
        calls = []

        def fn(texts):
            calls.append(1)
            raise T.FatalTranslationError("401 无效密钥")
        tr = FakeTranslator(fn=fn, retries=3)
        with self.assertRaises(T.FatalTranslationError):
            tr.translate_texts(["a"])
        self.assertEqual(len(calls), 1)


class OpenAIParseTests(unittest.TestCase):
    """模型返回的形状千奇百怪，解析要能兜住，条数不对时要能触发拆批。"""

    def _tr(self):
        return T.OpenAITranslator("https://x/v1", "sk-1", "m", retries=1)

    def test_accepts_plain_array(self):
        tr = self._tr()
        self.assertEqual(tr._parse_lines('["甲","乙"]', 2), ["甲", "乙"])

    def test_accepts_string_ids(self):
        tr = self._tr()
        raw = '{"lines":[{"id":"1","text":"甲"},{"id":"2","text":"乙"}]}'
        self.assertEqual(tr._parse_lines(raw, 2), ["甲", "乙"])

    def test_accepts_other_envelope(self):
        tr = self._tr()
        raw = '{"translations":[{"id":1,"text":"甲"}]}'
        self.assertEqual(tr._parse_lines(raw, 1), ["甲"])

    def test_count_mismatch_raises_shape_error(self):
        tr = self._tr()
        with self.assertRaises(T.ShapeError):
            tr._parse_lines('{"lines":[{"id":1,"text":"甲"}]}', 3)

    def test_broken_json_is_plain_translation_error(self):
        tr = self._tr()
        with self.assertRaises(T.TranslationError) as ctx:
            tr._parse_lines('{"lines":[{"id":1,"text":"甲"', 2)
        self.assertNotIsInstance(ctx.exception, T.ShapeError)

    def test_empty_translation_is_shape_error(self):
        tr = self._tr()
        with self.assertRaises(T.ShapeError):
            tr._parse_lines('{"lines":[{"id":1,"text":"  "}]}', 1)

    def test_batch_that_miscounts_gets_split_and_saved(self):
        # 上游对 4 条一起的请求只回 1 条（模型数不清），拆到单条就能对上
        tr = self._tr()
        tr.retries = 1
        tr._request = lambda texts: (["只有一条"] if len(texts) > 1 else [texts[0] + "·译"])
        out = tr.translate_texts(["a", "b", "c", "d"])
        self.assertEqual(out, ["a·译", "b·译", "c·译", "d·译"])

    def test_request_carries_line_count_and_max_tokens(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            items = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
            lines = [{"id": i["id"], "text": i["text"] + "译"} for i in items]
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"lines": lines}, ensure_ascii=False)}}]})

        tr = T.OpenAITranslator("https://x/v1", "sk-1", "m", retries=1)
        tr.client = _client(handler)
        self.assertEqual(tr.translate_texts(["あ", "い"]), ["あ译", "い译"])
        body = bodies[0]
        self.assertGreaterEqual(body["max_tokens"], 2048)
        self.assertIn("共 2 条", body["messages"][1]["content"])


class GoogleTests(unittest.TestCase):
    def test_request_shape(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["key"] = request.headers.get("x-goog-api-key")
            seen["ctype"] = request.headers.get("content-type")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json=[["早上好", "谢谢"]])

        tr = T.GoogleTranslator("ja", "zh-CN", retries=1)
        tr.client = _client(handler)
        out = tr.translate_texts(["おはよう", "ありがとう"])
        self.assertEqual(out, ["早上好", "谢谢"])
        self.assertTrue(seen["url"].endswith("/v1/translateHtml"))
        self.assertEqual(seen["ctype"], "application/json+protobuf")
        self.assertTrue(seen["key"])
        self.assertIn("おはよう", seen["body"])
        self.assertIn('"ja"', seen["body"])
        self.assertIn('"zh-CN"', seen["body"])

    def test_language_alias(self):
        self.assertEqual(T.GoogleTranslator("ja", "zh-cn").target_language, "zh-CN")
        self.assertEqual(T.BingTranslator("ja", "zh-CN").target_language, "zh-Hans")
        self.assertEqual(T.DeepLTranslator("k:fx", "ja", "zh-CN").target_language, "ZH-HANS")

    def test_403_triggers_one_key_refresh(self):
        keys = []

        def handler(request: httpx.Request):
            if request.url.path.endswith("/translateHtml"):
                key = request.headers.get("x-goog-api-key")
                keys.append(key)
                if key == T.GOOGLE_FALLBACK_KEY:
                    return httpx.Response(403, json={"error": "denied"})
                return httpx.Response(200, json=[["你好"]])
            # 抓新 key 的 js
            return httpx.Response(200, text='{"x-goog-api-key":"Abcdefghijklmnopqrstuvwxyz0123456789XYZ"}')

        tr = T.GoogleTranslator("ja", "zh-CN", retries=1)
        tr.client = _client(handler)
        out = tr.translate_texts(["こんにちは"])
        self.assertEqual(out, ["你好"])
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])


class BingTests(unittest.TestCase):
    def test_request_shape(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(200, json=[
                {"translations": [{"text": "早上好"}]},
                {"translations": [{"text": "谢谢"}]},
            ])

        tr = T.BingTranslator("ja", "zh-CN", retries=1)
        tr.client = _client(handler)
        out = tr.translate_texts(["おはよう", "ありがとう"])
        self.assertEqual(out, ["早上好", "谢谢"])
        self.assertIn("edge.microsoft.com", seen["url"])
        self.assertIn("to=zh-Hans", seen["url"])
        self.assertIn("from=ja", seen["url"])
        self.assertIn("おはよう", seen["body"])

    def test_malformed_response_becomes_translation_error(self):
        tr = T.BingTranslator("ja", "zh-CN", retries=1)
        tr.client = _client(lambda r: httpx.Response(200, json={"oops": 1}))
        with self.assertRaises(T.TranslationError):
            tr.translate_texts(["a"])


class DeepLTests(unittest.TestCase):
    def test_free_key_uses_api_free_endpoint(self):
        self.assertEqual(T.DeepLTranslator("abc:fx").endpoint, T.DeepLTranslator.FREE_ENDPOINT)
        self.assertEqual(T.DeepLTranslator("abc").endpoint, T.DeepLTranslator.PRO_ENDPOINT)

    def test_missing_key_raises_missing_config(self):
        tr = T.DeepLTranslator("")
        with self.assertRaises(T.MissingConfigError):
            tr.translate_texts(["a"])

    def test_request_shape(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"translations": [
                {"text": "早上好"}, {"text": "谢谢"}]})

        tr = T.DeepLTranslator("k:fx", "ja", "zh-CN", retries=1)
        tr.client = _client(handler)
        out = tr.translate_texts(["おはよう", "ありがとう"])
        self.assertEqual(out, ["早上好", "谢谢"])
        self.assertIn("api-free.deepl.com", seen["url"])
        self.assertEqual(seen["auth"], "DeepL-Auth-Key k:fx")
        self.assertIn("text=", seen["body"])
        self.assertIn("target_lang=ZH-HANS", seen["body"])

    def test_quota_exceeded_is_fatal(self):
        tr = T.DeepLTranslator("k:fx", retries=1)
        tr.client = _client(lambda r: httpx.Response(456, text="Quota exceeded"))
        with self.assertRaises(T.FatalTranslationError):
            tr.translate_texts(["a"])


class FactoryTests(unittest.TestCase):
    SETTINGS = {
        "translate_provider": "google",
        "source_language": "ja", "target_language": "zh-CN",
        "openai_base_url": "https://x/v1", "openai_api_key": "k", "openai_model": "m",
        "deepl_api_key": "d:fx", "translate_timeout": 30, "translate_retries": 2,
        "translate_rpm": 5, "translate_min_interval": 1.0, "translate_429_backoff": 60,
    }

    def test_provider_selects_engine(self):
        self.assertIsInstance(T.build_translator("google", self.SETTINGS), T.GoogleTranslator)
        self.assertIsInstance(T.build_translator("bing", self.SETTINGS), T.BingTranslator)
        self.assertIsInstance(T.build_translator("deepl", self.SETTINGS), T.DeepLTranslator)
        self.assertIsInstance(T.build_translator("openai", self.SETTINGS), T.OpenAITranslator)

    def test_cache_key_changes_with_provider_and_secret(self):
        a = T.translator_cache_key("google", self.SETTINGS)
        changed = dict(self.SETTINGS, translate_provider="bing")
        b = T.translator_cache_key("bing", changed)
        self.assertNotEqual(a, b)
        key1 = T.translator_cache_key("deepl", self.SETTINGS)
        key2 = T.translator_cache_key("deepl", dict(self.SETTINGS, deepl_api_key="other"))
        self.assertNotEqual(key1, key2)


class ConfigReadyTests(unittest.TestCase):
    """「翻译接口还没配好」这条提示不能对免密钥渠道误报。"""

    def _settings(self, **kw):
        from app.config import Settings
        base = dict(openai_base_url="https://x/v1", openai_api_key="", openai_model="m",
                    deepl_api_key="", google_api_key="",
                    # 默认备用渠道是 "openai,bing"，这里清空，单渠道行为才测得准
                    translate_fallback="")
        base.update(kw)
        return Settings(**base)

    def test_google_and_bing_are_always_ready(self):
        from app import pipeline
        self.assertTrue(pipeline.config_ready(self._settings(translate_provider="google")))
        self.assertTrue(pipeline.config_ready(self._settings(translate_provider="bing")))

    def test_default_fallback_keeps_queue_alive(self):
        # 默认顺序 deepl → openai → bing：deepl 没配 key 也不该把队列判成「没配置好」，
        # 因为链最末端是免密钥的必应
        from app import pipeline
        from app.config import Settings
        fresh = Settings()
        fresh.translate_provider = "deepl"
        fresh.deepl_api_key = ""
        fresh.openai_api_key = ""
        self.assertTrue(pipeline.config_ready(fresh))

    def test_deepl_needs_key(self):
        from app import pipeline
        self.assertFalse(pipeline.config_ready(self._settings(translate_provider="deepl")))
        self.assertTrue(pipeline.config_ready(
            self._settings(translate_provider="deepl", deepl_api_key="k:fx")))

    def test_openai_needs_key(self):
        from app import pipeline
        self.assertFalse(pipeline.config_ready(self._settings(translate_provider="openai")))
        self.assertTrue(pipeline.config_ready(
            self._settings(translate_provider="openai", openai_api_key="sk-x")))


class SecretNeverLeaksTests(unittest.TestCase):
    def test_safe_dict_masks_keys_except_deepl(self):
        """openai / google 掩码；DeepL 明文（页面要能一眼核对 :fx 抄全没）"""
        from app.config import Settings
        s = Settings(openai_api_key="a", google_api_key="b", deepl_api_key="c:fx")
        out = s.safe_dict()
        for field in ("openai_api_key", "google_api_key"):
            self.assertEqual(out[field], "********")
        self.assertEqual(out["deepl_api_key"], "c:fx")


if __name__ == "__main__":
    unittest.main()
