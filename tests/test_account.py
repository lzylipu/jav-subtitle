"""Web UI 里改登录账号与口令。

钉死的行为：
- 改账号要验当前口令，验不过不改（否则谁拿到浏览器都能改）
- 改完当场换一张会话票，不会把自己踢出去
- 旧 cookie 因为签名密钥变了立刻失效（这是设计，不是 bug）
- 环境变量强指定口令时，网页里改不动，接口要明确拒绝并说明原因
- 用户名/口令的长度下限、用户名不能带空格
- 老版本只有 admin-password.txt 的实例，启动时自动迁移到 account.json
"""
import json
import os
import unittest
from unittest import mock

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from fastapi.testclient import TestClient

from app import main


class AccountBase(unittest.TestCase):
    """注意：tests/env.py 为了别的用例全局设了 WEB_PASSWORD=test。

    这里必须把它摘掉，否则 web_credentials() 会认为「口令由环境变量指定」，
    一切都变成只读——那不是本文件要测的东西。
    """

    def setUp(self):
        self._env_password = os.environ.pop("WEB_PASSWORD", None)
        env.reset()
        main.reset_web_credentials()
        # 每个用例都从干净的文件状态开始
        for path in (main.ACCOUNT_FILE, main.PASSWORD_FILE):
            try:
                os.unlink(path)
            except OSError:
                pass
        self.client = TestClient(main.app, follow_redirects=False)
        self.user, self.password = main.web_credentials()

    def tearDown(self):
        main.reset_web_credentials()
        for path in (main.ACCOUNT_FILE, main.PASSWORD_FILE):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self._env_password is not None:
            os.environ["WEB_PASSWORD"] = self._env_password

    def login(self, password=None):
        return self.client.post("/api/login", json={
            "username": self.user, "password": password or self.password})


class AccountReadTests(AccountBase):
    def test_account_endpoint_requires_login(self):
        self.assertEqual(self.client.get("/api/account").status_code, 401)

    def test_account_endpoint_never_returns_password(self):
        self.login()
        r = self.client.get("/api/account")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["username"], self.user)
        self.assertNotIn("password", data)
        self.assertNotIn(self.password, r.text)


class ChangeCredentialTests(AccountBase):
    def test_change_password(self):
        self.login()
        r = self.client.post("/api/account", json={
            "username": self.user, "current_password": self.password,
            "password": "brand-new-pass-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(main.web_credentials()[1], "brand-new-pass-9")

    def test_change_password_without_old_password(self):
        # 页面上只留「用户名 + 新口令」两栏，不带旧口令也能改
        self.login()
        r = self.client.post("/api/account", json={
            "username": self.user, "password": "no-old-needed-1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(main.web_credentials()[1], "no-old-needed-1")

    def test_change_username_only_keeps_password(self):
        self.login()
        r = self.client.post("/api/account", json={
            "username": "kangna", "current_password": self.password, "password": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(main.web_credentials(), ("kangna", self.password))

    def test_wrong_current_password_is_rejected(self):
        self.login()
        r = self.client.post("/api/account", json={
            "username": self.user, "current_password": "not-it", "password": "another-pass-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(main.web_credentials()[1], self.password)

    def test_short_password_rejected(self):
        self.login()
        r = self.client.post("/api/account", json={
            "username": self.user, "current_password": self.password, "password": "1234567"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("8", r.json()["detail"])

    def test_username_with_space_rejected(self):
        self.login()
        r = self.client.post("/api/account", json={
            "username": "bad name", "current_password": self.password, "password": ""})
        self.assertEqual(r.status_code, 400)

    def test_new_session_cookie_is_issued_so_you_stay_logged_in(self):
        self.login()
        r = self.client.post("/api/account", json={
            "username": self.user, "current_password": self.password,
            "password": "another-pass-2"})
        self.assertIn(main.SESSION_COOKIE, r.headers.get("set-cookie", ""))
        # 换完票还能继续用
        self.assertEqual(self.client.get("/api/account").status_code, 200)

    def test_old_cookie_dies_after_password_change(self):
        self.login()
        old_cookie = self.client.cookies.get(main.SESSION_COOKIE)
        self.assertIsNotNone(old_cookie)
        self.client.cookies.clear()
        self.client.cookies.set(main.SESSION_COOKIE, old_cookie)
        r = self.client.get("/api/account")
        self.assertEqual(r.status_code, 200)          # 改之前是有效的
        self.client.post("/api/account", json={
            "username": self.user, "current_password": self.password,
            "password": "yet-another-3"})
        self.client.cookies.clear()
        self.client.cookies.set(main.SESSION_COOKIE, old_cookie)
        self.assertEqual(self.client.get("/api/account").status_code, 401)

    def test_credentials_survive_restart(self):
        self.login()
        self.client.post("/api/account", json={
            "username": "kangna", "current_password": self.password,
            "password": "persisted-pass-4"})
        main.reset_web_credentials()                  # 模拟重启：清内存缓存
        self.assertEqual(main.web_credentials(), ("kangna", "persisted-pass-4"))

    def test_account_file_is_private(self):
        self.login()
        self.client.post("/api/account", json={
            "username": self.user, "current_password": self.password,
            "password": "perm-check-pass-5"})
        mode = os.stat(main.ACCOUNT_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))


class EnvLockedTests(AccountBase):
    def test_env_password_cannot_be_changed_from_ui(self):
        with mock.patch.dict(os.environ, {"WEB_PASSWORD": "from-env-123"}):
            main.reset_web_credentials()
            user, password = main.web_credentials()
            self.assertEqual(password, "from-env-123")
            self.assertTrue(main.account_locked_reason())
            self.client.post("/api/login", json={"username": user, "password": password})
            r = self.client.post("/api/account", json={
                "username": user, "current_password": password, "password": "wanna-change-6"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("WEB_PASSWORD", r.json()["detail"])
        main.reset_web_credentials()


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self._env_password = os.environ.pop("WEB_PASSWORD", None)
        env.reset()
        for path in (main.ACCOUNT_FILE, main.PASSWORD_FILE):
            try:
                os.unlink(path)
            except OSError:
                pass
        main.reset_web_credentials()

    def tearDown(self):
        main.reset_web_credentials()
        for path in (main.ACCOUNT_FILE, main.PASSWORD_FILE):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self._env_password is not None:
            os.environ["WEB_PASSWORD"] = self._env_password

    def test_old_password_file_is_migrated(self):
        main.PASSWORD_FILE.write_text("legacy-pass-7\n", encoding="utf-8")
        self.assertEqual(main.web_credentials(), ("admin", "legacy-pass-7"))
        self.assertTrue(main.ACCOUNT_FILE.exists())
        data = json.loads(main.ACCOUNT_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["password"], "legacy-pass-7")

    def test_fresh_start_only_writes_account_json(self):
        # 首启写的是 account.json；admin-password.txt 是老版本的文件，不该再生成。
        # 启动横幅（docker logs）也必须指向实际存在的那个文件，否则用户去改会改空。
        user, password = main.web_credentials()
        self.assertTrue(main.ACCOUNT_FILE.exists())
        self.assertFalse(main.PASSWORD_FILE.exists())
        self.assertTrue(password)
        self.assertEqual(main.web_password_source(), "new")

    def test_broken_account_file_falls_back_instead_of_crashing(self):
        main.ACCOUNT_FILE.write_text("{not json", encoding="utf-8")
        user, password = main.web_credentials()      # 不抛异常，退回自动生成
        self.assertTrue(user)
        self.assertTrue(password)


if __name__ == "__main__":
    unittest.main()
