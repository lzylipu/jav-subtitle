"""刮削：只走 SubtitleCat，但要有限速闸门。

三个必须锁住的行为：
1. 平台不再让用户选（只有 subtitlecat 实测可用），界面上也不该再冒出
   「平台顺序 / SubDL key / OpenSubtitles key」这些填了也没用的框；
2. 两次刮削之间至少隔 N 秒，否则连刷同一个站会被封 IP；
3. 每小时最多刮 N 部，超了就先转写，不许一次入队 2000 部把站方惹毛。
"""
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import env  # noqa: F401  (设置 DATA_DIR 等环境变量)

from app import main as app_main
from app import scraping
from app.config import Settings
from app.db import init_db, load_settings, save_settings

HTML = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"


def scrape_block() -> str:
    html = HTML.read_text(encoding="utf-8")
    start = html.index("刮削字幕")
    return html[start:html.index("</fieldset>", start)]


class DefaultsTest(unittest.TestCase):
    def test_only_subtitlecat_by_default(self):
        fresh = Settings()
        self.assertEqual(scraping.parse_order(fresh.scrape_providers), ["subtitlecat"])
        self.assertEqual(scraping.default_order(), ["subtitlecat"])

    def test_legacy_config_cannot_reenable_dead_sites(self):
        # 老库里存着 "subtitlecat,kitsunekko"，接口层面也只认 subtitlecat
        self.assertEqual(scraping.active_order("subtitlecat,kitsunekko"), ["subtitlecat"])
        self.assertEqual(scraping.active_order("subdl,opensubtitles"), ["subtitlecat"])
        self.assertEqual(scraping.active_order(""), ["subtitlecat"])

    def test_limits_are_conservative(self):
        fresh = Settings()
        self.assertGreaterEqual(fresh.scrape_min_interval, 10)
        self.assertGreater(fresh.scrape_hourly_limit, 0)


class FormTest(unittest.TestCase):
    def test_platform_choice_is_gone(self):
        block = scrape_block()
        for gone in ('name="scrape_providers"', 'name="subdl_api_key"',
                     'name="opensubtitles_api_key"'):
            self.assertNotIn(gone, block, f"刮削区不该再有 {gone}（填了也没用）")

    def test_scrape_is_always_on(self):
        """刮削必走，界面不再有开关；配置层默认就是 True"""
        self.assertNotIn('name="scrape_enabled"', scrape_block())
        self.assertNotIn("开启刮削", scrape_block())
        self.assertTrue(Settings().scrape_enabled)

    def test_limits_are_editable(self):
        block = scrape_block()
        self.assertIn('name="scrape_min_interval"', block)
        self.assertIn('name="scrape_hourly_limit"', block)

    def test_test_button_and_status_still_there(self):
        block = scrape_block()
        self.assertIn('id="scrapeTest"', block)
        self.assertIn('id="scrapeOut"', block)
        self.assertIn('id="scrapeList"', block)

    def test_limits_are_patchable(self):
        fields = set(app_main.SettingsPatch.model_fields)
        for name in ("scrape_min_interval", "scrape_hourly_limit"):
            self.assertIn(name, fields)


class GateTest(unittest.TestCase):
    def setUp(self):
        scraping.GATE.reset()
        scraping.PROVIDER_CLASSES  # 确保模块已就绪
        self.calls = []

        outer = self

        class FakeCat:
            needs_key = False

            def __init__(self, settings):
                pass

            def available(self):
                return True, ""

            def search(self, query):
                outer.calls.append("search")
                return []                     # 没搜到 → 不下载，只验闸门

            def language_of(self, candidate):
                return "zh"

        self._orig = dict(scraping.PROVIDER_CLASSES)
        scraping.PROVIDER_CLASSES.update({"subtitlecat": FakeCat})
        self.settings = SimpleNamespace(
            scrape_enabled=True,
            scrape_providers="subtitlecat",
            scrape_prefer="zh",
            scrape_min_interval=30,
            scrape_hourly_limit=2,
        )

    def tearDown(self):
        scraping.PROVIDER_CLASSES.clear()
        scraping.PROVIDER_CLASSES.update(self._orig)
        scraping.GATE.reset()

    def test_second_call_within_interval_is_refused_without_waiting(self):
        scraping.scrape("PPPD-424.mp4", self.settings, wait=False)
        started = time.time()
        second = scraping.scrape("PPPD-425.mp4", self.settings, wait=False)
        self.assertLess(time.time() - started, 1.0, "wait=False 不许真睡")
        self.assertFalse(second.ok)
        self.assertTrue(any("还要等" in n for n in second.notes), second.notes)

    def test_wait_true_actually_sleeps(self):
        scraping.scrape("PPPD-424.mp4", self.settings, wait=False)
        self.settings.scrape_min_interval = 0.6
        started = time.time()
        scraping.scrape("PPPD-425.mp4", self.settings, wait=True)
        self.assertGreaterEqual(time.time() - started, 0.55)

    def test_hourly_limit_blocks_and_says_why(self):
        self.settings.scrape_min_interval = 0
        scraping.scrape("a.mp4", self.settings, wait=False)
        scraping.scrape("b.mp4", self.settings, wait=False)
        third = scraping.scrape("c.mp4", self.settings, wait=False)
        self.assertFalse(third.ok)
        self.assertTrue(any("上限" in n for n in third.notes), third.notes)
        self.assertEqual(len(self.calls), 2, "达到上限后不该再发请求")

    def test_zero_limit_means_unlimited(self):
        self.settings.scrape_min_interval = 0
        self.settings.scrape_hourly_limit = 0
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            scraping.scrape(name, self.settings, wait=False)
        self.assertEqual(len(self.calls), 3)

    def test_snapshot_reports_usage(self):
        scraping.scrape("a.mp4", self.settings, wait=False)
        snap = scraping.GATE.snapshot()
        self.assertEqual(snap["used_last_hour"], 1)
        self.assertIsNotNone(snap["seconds_since_last"])


class StatusEndpointTest(unittest.TestCase):
    def setUp(self):
        init_db()
        save_settings(Settings())
        scraping.GATE.reset()

    def test_status_exposes_limits_and_gate(self):
        r = app_main.scrape_status()
        self.assertEqual(r["interval"], Settings().scrape_min_interval)
        self.assertEqual(r["hourly_limit"], Settings().scrape_hourly_limit)
        self.assertIn("used_last_hour", r["gate"])
        self.assertEqual(r["order"], ["subtitlecat"])

    def test_patch_limits_persist(self):
        app_main.set_settings(app_main.SettingsPatch(
            scrape_min_interval=15, scrape_hourly_limit=25))
        now = load_settings()
        self.assertEqual((now.scrape_min_interval, now.scrape_hourly_limit), (15.0, 25))

    def test_saving_collapses_legacy_provider_list(self):
        # 老库里是 "subtitlecat,kitsunekko"；保存任意一项后就该收敛成 subtitlecat
        init_db()
        save_settings(Settings.from_dict({**Settings().__dict__,
                                          "scrape_providers": "subtitlecat,kitsunekko"}))
        app_main.set_settings(app_main.SettingsPatch(daily_char_budget=1000))
        self.assertEqual(load_settings().scrape_providers, "subtitlecat")


if __name__ == "__main__":
    unittest.main()
