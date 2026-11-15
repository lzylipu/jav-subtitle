"""登录页与会话 cookie：不再依赖浏览器原生 Basic 弹窗。

要点：
- `/login` 匿名可访问，`/api/login` 匿名可调用
- 没登录时：页面请求跳转 303 到 /login，接口请求回 401 JSON，且**不带
  WWW-Authenticate: Basic**（带了浏览器就会弹原生对话框，正是要根除的东西）
- 登录成功种 HttpOnly Cookie，重启容器不掉（签名里带过期时间，密钥来自口令）
- 改口令后旧 Cookie 立刻失效
"""
import os
import unittest
from unittest import mock

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from fastapi.testclient import TestClient

from app import main


class LoginTests(unittest.TestCase):
    def setUp(self):
        env.reset()                 # 建表，否则 load_settings 会炸
        main.reset_web_credentials()
        self.client = TestClient(main.app, follow_redirects=False)

    def tearDown(self):
        main.reset_web_credentials()

    def test_login_page_is_anonymous(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("登录", r.text)
        self.assertIn("admin", r.text)

    def test_home_redirects_to_login_when_anonymous(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_api_returns_401_json_without_basic_challenge(self):
        r = self.client.get("/api/stats")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("www-authenticate", {k.lower() for k in r.headers})

    def test_login_with_right_password_sets_cookie(self):
        _, password = main.web_credentials()
        r = self.client.post("/api/login", json={"username": "admin", "password": password})
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("set-cookie", "")
        self.assertIn(main.SESSION_COOKIE, cookie)
        self.assertIn("HttpOnly", cookie)

    def test_login_with_wrong_password_rejected(self):
        r = self.client.post("/api/login", json={"username": "admin", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn(main.SESSION_COOKIE, r.headers.get("set-cookie", ""))

    def test_session_cookie_grants_access(self):
        _, password = main.web_credentials()
        self.client.post("/api/login", json={"username": "admin", "password": password})
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/device").status_code, 200)

    def test_logout_drops_session(self):
        _, password = main.web_credentials()
        self.client.post("/api/login", json={"username": "admin", "password": password})
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/").status_code, 303)

    def test_anonymous_paths_bypass_auth(self):
        for path in ("/login",):
            self.assertNotEqual(self.client.get(path).status_code, 401)


class SessionTokenTests(unittest.TestCase):
    def setUp(self):
        # 别用 pop 收尾：会连 env.py 设的 WEB_PASSWORD 一起删掉，污染后续测试
        self._env = mock.patch.dict(os.environ, {"WEB_PASSWORD": "test-pass-123"})
        self._env.start()
        main.reset_web_credentials()

    def tearDown(self):
        self._env.stop()
        main.reset_web_credentials()

    def _set_password(self, value: str) -> None:
        os.environ["WEB_PASSWORD"] = value
        main.reset_web_credentials()

    def test_roundtrip(self):
        self.assertTrue(main.verify_session(main.make_session()))

    def test_garbage_rejected(self):
        for bad in ("", "abc", "123.", ".abc", "abc.def", "99999999999.x"):
            self.assertFalse(main.verify_session(bad), bad)

    def test_expired_rejected(self):
        with mock.patch.object(main.time, "time", return_value=1_000_000.0):
            token = main.make_session()
        with mock.patch.object(main.time, "time",
                               return_value=1_000_000.0 + main.SESSION_TTL + 10):
            self.assertFalse(main.verify_session(token))

    def test_changing_password_invalidates_old_cookie(self):
        token = main.make_session()
        self.assertTrue(main.verify_session(token))
        self._set_password("another-pass")
        self.assertFalse(main.verify_session(token))

    def test_tampered_signature_rejected(self):
        expires, sig = main.make_session().split(".", 1)
        # 逐位翻转第一个字符，保证改动一定发生（原来把末两位替换成 "00"，
        # 签名本来就以 "00" 结尾时测试会假失败）
        head = "1" if sig[0] != "1" else "2"
        self.assertFalse(main.verify_session(expires + "." + head + sig[1:]))


if __name__ == "__main__":
    unittest.main()
