"""品牌与界面细节：logo、favicon、分组标题、保存按钮、反馈时长。

这几条都是「单测全绿但用户一眼就看出不对」的地方：
- logo 必须匿名可访问，否则未登录的登录页上是个破图
- 分组标题不许再骑在卡片边框线上（用 .fsTitle，不再用 <legend>）
- 三个表单的按钮统一叫「保存」
- 常驻的黄条 #notice 已经删掉，反馈只走 3 秒自动消失的 toast
"""
import unittest

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from fastapi.testclient import TestClient

from app import main


class BrandingTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        main.reset_web_credentials()
        self.client = TestClient(main.app, follow_redirects=False)

    def tearDown(self):
        main.reset_web_credentials()

    def _login(self) -> TestClient:
        _, password = main.web_credentials()
        client = TestClient(main.app, follow_redirects=False)
        r = client.post("/api/login", json={"username": "admin", "password": password})
        self.assertEqual(r.status_code, 200)
        return client

    # ---- 静态图标 ----------------------------------------------------
    def test_logo_is_reachable_without_login(self):
        r = self.client.get("/static/logo.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.content.startswith(b"\x89PNG"))
        # 太小的图说明误拷了占位图
        self.assertGreater(len(r.content), 100 * 1024)

    def test_favicon_and_touch_icon_are_reachable_without_login(self):
        for name in ("/static/favicon.png", "/static/apple-touch-icon.png"):
            r = self.client.get(name)
            self.assertEqual(r.status_code, 200, name)
            self.assertTrue(r.content.startswith(b"\x89PNG"), name)

    def test_icon_cache_header(self):
        r = self.client.get("/static/logo.png")
        self.assertIn("max-age", r.headers.get("cache-control", ""))

    def test_unknown_static_file_is_404_not_a_path_traversal(self):
        client = self._login()
        self.assertEqual(client.get("/static/nope.png").status_code, 404)
        self.assertEqual(client.get("/static/../main.py").status_code, 404)

    # ---- 页面里真的引用了 logo ---------------------------------------
    def test_login_page_uses_logo_and_favicon(self):
        r = self.client.get("/login")
        self.assertIn('src="/static/logo.png"', r.text)
        self.assertIn('href="/static/favicon.png"', r.text)
        # 老的 data:image/svg 占位图标不该再出现
        self.assertNotIn("data:image/svg+xml", r.text)

    def test_index_page_uses_logo_and_favicon(self):
        r = self._login().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('src="/static/logo.png"', r.text)
        self.assertIn('href="/static/favicon.png"', r.text)

    # ---- 设置页排版 --------------------------------------------------
    def test_fieldset_titles_are_not_legend_on_the_border(self):
        html = self._login().get("/").text
        self.assertNotIn("<legend>", html)
        self.assertIn('class="fsTitle"', html)
        self.assertIn(".fsTitle{grid-column:1/-1", html)

    def test_all_save_buttons_are_named_save(self):
        # v3.10：整页只剩标题右侧那一个保存入口，各分组不再单独挂保存按钮
        html = self._login().get("/").text
        self.assertIn('<button class="primary" type="button" id="setSave">保存</button>', html)
        for old in ("保存设置", "保存账号口令", "保存接入设置"):
            self.assertNotIn(old, html)

    def test_no_persistent_notice_banner(self):
        html = self._login().get("/").text
        self.assertNotIn('id="notice"', html)
        self.assertNotIn("#notice", html)

    def test_toast_disappears_after_three_seconds(self):
        html = self._login().get("/").text
        self.assertIn("const life = ms || 3000;", html)
