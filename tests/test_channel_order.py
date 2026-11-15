"""翻译渠道：三档顺序 + 「测试」必须测你眼前选的那一档。

三个真出过的问题锁在这里：
1. 页面上选了 openai 点「测试」，回来的却是 deepl —— 因为测试接口没带渠道，
   服务端拿的是上次保存进库的 translate_provider；
2. 表单里刚填的 key 没参与测试，测出来的还是库里的旧 key；
3. 顺序只能存主渠道 + 备用渠道两个值，第三档无处安放、重复渠道也没去干净。
"""
import re
import unittest
from pathlib import Path
from unittest import mock

from tests import env  # noqa: F401  (设置 DATA_DIR 等环境变量)

from app import main as app_main
from app.config import Settings
from app.db import init_db, load_settings, save_settings
from app.translation import build_translator, parse_fallback

HTML = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"


def settings_block() -> str:
    html = HTML.read_text(encoding="utf-8")
    start = html.index('<form id="settings">')
    return html[start:html.index("</form>", start)]


class ChannelOrderTest(unittest.TestCase):
    """三档顺序：第 1 档存 translate_provider，第 2、3 档存 translate_fallback。"""

    def setUp(self):
        init_db()
        save_settings(Settings())

    def test_three_slots_roundtrip(self):
        app_main.set_settings(app_main.SettingsPatch(
            translate_provider="deepl", translate_fallback="openai,bing"))
        now = load_settings()
        chain = [now.translate_provider] + parse_fallback(now.translate_fallback)
        self.assertEqual(chain, ["deepl", "openai", "bing"])

    def test_same_channel_twice_is_dropped(self):
        # 用户把 deepl 同时排在第 1、2 档：存进去时要自动去重，别让引擎重复造同一个客户端
        app_main.set_settings(app_main.SettingsPatch(
            translate_provider="deepl", translate_fallback="deepl,bing"))
        now = load_settings()
        self.assertEqual(now.translate_fallback, "bing")

    def test_order_survives_resave_of_other_fields(self):
        app_main.set_settings(app_main.SettingsPatch(
            translate_provider="deepl", translate_fallback="openai,bing"))
        app_main.set_settings(app_main.SettingsPatch(daily_char_budget=120000))
        now = load_settings()
        self.assertEqual(now.translate_fallback, "openai,bing")

    def test_defaults_are_deepl_then_openai_then_bing(self):
        fresh = Settings()
        self.assertEqual(fresh.translate_provider, "deepl")
        self.assertEqual(parse_fallback(fresh.translate_fallback), ["openai", "bing"])


class ChannelMarkupTest(unittest.TestCase):
    """页面上确实有三档，且每档都有自己的测试按钮。"""

    def test_three_picks_and_three_test_buttons(self):
        block = settings_block()
        self.assertEqual(block.count('class="chPick"'), 3)
        self.assertEqual(block.count('class="ghost chTest"'), 3)
        # 每档 5 个 data-slot：档位容器 / 下拉 / 输出行 / 配置区 / 测试按钮
        self.assertEqual(len(re.findall(r'data-slot="\d"', block)), 15)

    def test_only_first_slot_is_the_primary_field(self):
        block = settings_block()
        named = re.findall(r'<select class="chPick"[^>]*name="([a-z_]+)"', block)
        self.assertEqual(named, ["translate_provider"])
        self.assertEqual(block.count('name="translate_fallback"'), 1)

    def test_config_blocks_live_in_bank_until_moved(self):
        # 配置块必须留在表单里（被 JS 整块搬进某一档），否则 collectForm 收不到值
        block = settings_block()
        self.assertIn('id="cfgBank"', block)
        for name in ("openai_base_url", "openai_api_key", "openai_model",
                     "deepl_api_key", "deepl_api_url", "google_api_key"):
            self.assertIn(f'name="{name}"', block)

    def test_element_ids_the_js_touches_exist(self):
        block = settings_block()
        for el_id in ("cfgBank", "testChain", "testChainOut"):
            self.assertIn(f'id="{el_id}"', block)


class TestEndpointHonorsFormTest(unittest.TestCase):
    """「测试」测的是页面上选的那一档 + 页面上填的那把 key。"""

    def setUp(self):
        init_db()
        save_settings(app_main.Settings.from_dict({
            **Settings().__dict__,
            "translate_provider": "deepl",
            "translate_fallback": "",
            "deepl_api_key": "saved-deepl-key:fx",
            "openai_api_key": "",
            "openai_base_url": "https://saved.example/v1",
            "openai_model": "saved-model",
        }))
        self.sent = []

        class FakeEngine:
            last_misses: list = []

            def translate_texts(self, texts):
                return ["你好"]

            def key_report(self):
                return ""

            def usage(self):
                return {"ok": False, "detail": ""}

            def close(self):
                pass

        def fake_build(provider, settings, stop_event=None):
            self.sent.append((provider, settings))
            return FakeEngine()

        self.fake = FakeEngine
        self.patcher = mock.patch.object(app_main, "build_translator", fake_build)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_provider_from_body_wins_over_saved(self):
        r = app_main.translate_test(app_main.TranslateTestBody(provider="openai"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["provider"], "openai")
        self.assertEqual(self.sent[0][0], "openai")

    def test_form_key_reaches_the_engine(self):
        app_main.translate_test(app_main.TranslateTestBody(
            provider="openai", openai_api_key="sk-typed-in-form",
            openai_base_url="https://typed.example/v1", openai_model="typed-model"))
        _, eff = self.sent[0]
        self.assertEqual(eff.openai_api_key, "sk-typed-in-form")
        self.assertEqual(eff.openai_base_url, "https://typed.example/v1")
        self.assertEqual(eff.openai_model, "typed-model")

    def test_blank_or_masked_form_value_keeps_saved(self):
        app_main.translate_test(app_main.TranslateTestBody(
            provider="deepl", deepl_api_key="", openai_api_key="********"))
        _, eff = self.sent[0]
        self.assertEqual(eff.deepl_api_key, "saved-deepl-key:fx")
        self.assertEqual(eff.openai_api_key, "")

    def test_saved_settings_not_mutated_by_test(self):
        app_main.translate_test(app_main.TranslateTestBody(
            provider="deepl", deepl_api_key="only-for-this-test:fx"))
        self.assertEqual(load_settings().deepl_api_key, "saved-deepl-key:fx")

    def test_no_body_uses_saved_provider(self):
        app_main.translate_test(None)
        self.assertEqual(self.sent[0][0], "deepl")

    def test_testing_one_slot_disables_the_chain(self):
        # 库里配了 deepl→openai→bing。测 deepl 那一档时必须只测 deepl，
        # 否则 key 是空的会自动落到必应，页面显示「deepl 正常」骗人。
        app_main.set_settings(app_main.SettingsPatch(
            translate_provider="deepl", translate_fallback="openai,bing"))
        app_main.translate_test(app_main.TranslateTestBody(provider="deepl"))
        _, eff = self.sent[0]
        self.assertEqual(eff.translate_fallback, "")
        self.assertEqual(load_settings().translate_fallback, "openai,bing")


class ChainTestWiringTest(unittest.TestCase):
    """三档顺序真的会进到引擎的降级链里。"""

    def test_build_translator_gets_all_three_in_order(self):
        s = Settings(translate_provider="deepl", translate_fallback="openai,bing",
                     deepl_api_key="k:fx", openai_api_key="sk-x", openai_base_url="http://x/v1")
        engine = build_translator("deepl", s)
        names = [name for name, _ in getattr(engine, "entries", [("deepl", engine)])]
        self.assertEqual(names, ["deepl", "openai", "bing"])
        engine.close()


if __name__ == "__main__":
    unittest.main()
