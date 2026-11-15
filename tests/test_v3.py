"""v3 新增能力测试：目录索引、按目录入队、限流闸门、AI token 鉴权。"""
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import env

from app import pipeline
from app.config import Settings
from app.db import connect, load_settings, save_settings
from app.ratelimit import RateLimiter
from app.scanner import library_tree, scan_media


class LibraryTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = load_settings()
        save_settings(Settings(media_root=str(self.tmp), min_file_age=0, target_language="zh-CN"))

    def tearDown(self):
        save_settings(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, rel: str) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_library_counts_are_recursive(self):
        self._mk("A/1.mp4")
        self._mk("A/2.mp4")
        self._mk("A/B/3.mp4")
        self._mk("C/4.mp4")
        self._mk("C/5.mp4")
        self._mk("C/6.mp4")
        scan_media(str(self.tmp))

        root = library_tree(str(self.tmp))
        self.assertEqual(root["library_total"]["videos"], 6)
        names = {c["name"]: c for c in root["children"]}
        self.assertEqual(names["A"]["videos"], 3, "A 应把子目录 B 一起算进去")
        self.assertEqual(names["C"]["videos"], 3)
        self.assertEqual(set(names), {"A", "C"})

        child = library_tree(str(self.tmp), str(self.tmp / "A"))
        self.assertEqual(child["self"]["videos"], 3)
        self.assertEqual(child["self"]["direct"], 2, "A 直属 2 部，B 里 1 部")
        self.assertEqual([c["name"] for c in child["children"]], ["B"])

    def test_library_rejects_outside_path(self):
        outside = Path(tempfile.mkdtemp())
        try:
            with self.assertRaises(ValueError):
                library_tree(str(self.tmp), str(outside))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_enqueue_only_selected_directory(self):
        self._mk("S1/1.mp4")
        self._mk("S1/2.mp4")
        self._mk("S2/3.mp4")
        scan_media(str(self.tmp))
        scan_media(str(self.tmp))

        queued = pipeline.queue_indexed(str(self.tmp / "S1"), force=True)
        self.assertEqual(queued, 2, "只应入队 S1 下的两部")
        with connect() as conn:
            paths = [r["video_path"] for r in conn.execute("SELECT video_path FROM jobs")]
        self.assertTrue(all("/S1/" in p for p in paths), paths)

    def test_force_enqueue_ignores_stability_gate(self):
        """用户手动点「开始」时不该被 120 秒稳定闸门卡住。"""
        self._mk("new/just-copied.mp4")
        scan_media(str(self.tmp))
        self.assertEqual(pipeline.queue_indexed(str(self.tmp)), 0, "未稳定时默认不入队")
        self.assertEqual(pipeline.queue_indexed(str(self.tmp), force=True), 1)

    def test_library_shows_job_state(self):
        self._mk("D/1.mp4")
        self._mk("D/2.mp4")
        scan_media(str(self.tmp))
        scan_media(str(self.tmp))
        pipeline.queue_indexed(str(self.tmp / "D"), force=True)
        with connect() as conn:
            first = conn.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()["id"]
            conn.execute("UPDATE jobs SET status='done' WHERE id=?", (first,))
        node = library_tree(str(self.tmp), str(self.tmp / "D"))
        self.assertEqual(node["self"]["done"], 1)
        self.assertEqual(node["self"]["pending"], 1)

    def test_enqueue_limit_only_takes_first_n(self):
        for i in range(6):
            self._mk(f"L/{i}.mp4")
        scan_media(str(self.tmp))
        self.assertEqual(pipeline.queue_indexed(str(self.tmp), force=True, limit=2), 2)
        self.assertEqual(pipeline.queue_indexed(str(self.tmp), force=True), 4,
                         "第二次不带 limit 应把剩下的 4 部补齐，不会重复")

    def test_scan_does_not_auto_enqueue_by_default(self):
        """默认扫描只建索引：不能替用户把整库排队。"""
        from app.config import Settings as S
        self.assertFalse(S().auto_enqueue)


class RateLimiterTests(unittest.TestCase):
    def test_min_interval_is_enforced(self):
        limiter = RateLimiter()
        started = time.time()
        for _ in range(3):
            self.assertTrue(limiter.acquire(rpm=0, min_interval=0.2))
        elapsed = time.time() - started
        self.assertGreaterEqual(elapsed, 0.38, "三次请求至少间隔 0.4 秒，不能突发")

    def test_rpm_converts_to_spacing(self):
        limiter = RateLimiter()
        started = time.time()
        limiter.acquire(rpm=600, min_interval=0)   # 0.1 秒一次
        limiter.acquire(rpm=600, min_interval=0)
        self.assertGreaterEqual(time.time() - started, 0.09)

    def test_429_sets_global_cooldown(self):
        limiter = RateLimiter()
        seconds = limiter.note_429(retry_after=5, default_backoff=120)
        self.assertEqual(seconds, 5.0, "优先听 Retry-After")
        snapshot = limiter.snapshot()
        self.assertGreater(snapshot["cooldown_remaining"], 0)
        started = time.time()
        limiter.acquire(rpm=0, min_interval=0)     # 应被冷却挡住
        self.assertGreaterEqual(time.time() - started, 4.5)

    def test_repeated_429_backs_off_exponentially(self):
        limiter = RateLimiter()
        self.assertEqual(limiter.note_429(None, 10), 10)
        self.assertEqual(limiter.note_429(None, 10), 20)
        self.assertEqual(limiter.note_429(None, 10), 40)

    def test_cooldown_is_capped(self):
        limiter = RateLimiter()
        for _ in range(12):
            seconds = limiter.note_429(None, 60)
        self.assertLessEqual(seconds, 1800)


class AuthTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        self.saved = load_settings()
        settings = Settings(api_token="hs_testtoken", api_token_readonly=False)
        save_settings(settings)

    def tearDown(self):
        save_settings(self.saved)

    @staticmethod
    def _request(method="GET", token=None, basic=None):
        from starlette.requests import Request
        import base64
        headers = []
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if basic:
            raw = base64.b64encode(f"{basic[0]}:{basic[1]}".encode())
            headers.append((b"authorization", b"Basic " + raw))
        return Request({"type": "http", "method": method, "headers": headers,
                        "path": "/api/stats", "query_string": b""})

    def test_bearer_token_accepted(self):
        from app.main import authorized
        authorized(self._request(token="hs_testtoken"), None)

    def test_wrong_token_rejected(self):
        from app.main import authorized
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            authorized(self._request(token="nope"), None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_readonly_token_cannot_write(self):
        from app.main import authorized
        from fastapi import HTTPException
        save_settings(Settings(api_token="hs_ro", api_token_readonly=True))
        authorized(self._request("GET", token="hs_ro"), None)
        with self.assertRaises(HTTPException) as ctx:
            authorized(self._request("POST", token="hs_ro"), None)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_basic_auth_still_works(self):
        from app.main import authorized
        from fastapi.security import HTTPBasicCredentials
        creds = HTTPBasicCredentials(username="admin", password="test")
        authorized(self._request(basic=("admin", "test")), creds)

    def test_basic_auth_wrong_password_rejected(self):
        from app.main import authorized
        from fastapi import HTTPException
        from fastapi.security import HTTPBasicCredentials
        creds = HTTPBasicCredentials(username="admin", password="wrong")
        with self.assertRaises(HTTPException) as ctx:
            authorized(self._request(basic=("admin", "wrong")), creds)
        self.assertEqual(ctx.exception.status_code, 401)


class SafetyTests(unittest.TestCase):
    def test_refuses_to_write_non_subtitle(self):
        """安全边界：这个函数绝不能写视频文件。"""
        from app.subtitles import atomic_write
        tmp = Path(tempfile.mkdtemp())
        try:
            with self.assertRaises(ValueError):
                atomic_write(tmp / "movie.mp4", "x")
            self.assertFalse((tmp / "movie.mp4").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_writes_srt_next_to_video(self):
        from app.subtitles import atomic_write
        tmp = Path(tempfile.mkdtemp())
        try:
            target = tmp / "movie.zh-CN.srt"
            atomic_write(target, "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
            self.assertTrue(target.exists())
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["movie.zh-CN.srt"],
                             "不能留临时文件")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DefaultSettingsTests(unittest.TestCase):
    def test_workers_default_to_one(self):
        s = Settings()
        self.assertEqual((s.extract_workers, s.asr_workers, s.translate_workers), (1, 1, 1))

    def test_no_secret_in_defaults(self):
        s = Settings()
        self.assertEqual(s.openai_api_key, "")
        self.assertEqual(s.api_token, "")

    def test_rate_limit_defaults_are_conservative(self):
        s = Settings()
        self.assertGreater(s.translate_min_interval, 0)
        self.assertLessEqual(s.translate_rpm, 60)


class AutoPasswordTests(unittest.TestCase):
    """没配 WEB_PASSWORD 也要能起来：自动生成一次、落盘复用、不因缺配置拒绝启动。"""

    def setUp(self):
        from app import main
        self.main = main
        env.reset()
        main.reset_web_credentials()
        self.tmp = Path(tempfile.mkdtemp())
        self.pwfile = self.tmp / "account.json"
        self._orig_file = main.PASSWORD_FILE
        self._orig_account = main.ACCOUNT_FILE
        main.ACCOUNT_FILE = self.pwfile
        main.PASSWORD_FILE = self.tmp / "legacy-password.txt"   # 确保迁移分支不参与
        self._orig_env = os.environ.pop("WEB_PASSWORD", None)

    def tearDown(self):
        self.main.PASSWORD_FILE = self._orig_file
        self.main.ACCOUNT_FILE = self._orig_account
        if self._orig_env is not None:
            os.environ["WEB_PASSWORD"] = self._orig_env
        else:
            os.environ.pop("WEB_PASSWORD", None)
        self.main.reset_web_credentials()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generates_password_when_env_missing(self):
        user, password = self.main.web_credentials()
        self.assertEqual(user, "admin")
        self.assertGreaterEqual(len(password), 12)
        self.assertEqual(self.main.web_password_source(), "new")
        stored = json.loads(self.pwfile.read_text(encoding="utf-8"))
        self.assertEqual(stored["password"], password)
        self.assertEqual(stored["username"], "admin")

    def test_reuses_password_file_across_restart(self):
        _, first = self.main.web_credentials()
        self.main.reset_web_credentials()
        _, second = self.main.web_credentials()
        self.assertEqual(first, second)
        self.assertEqual(self.main.web_password_source(), "file")

    def test_env_password_wins_over_file(self):
        self.pwfile.write_text(json.dumps({"username": "admin", "password": "fromfile"}),
                               encoding="utf-8")
        os.environ["WEB_PASSWORD"] = "fromenv"
        try:
            self.assertEqual(self.main.web_credentials()[1], "fromenv")
            self.assertEqual(self.main.web_password_source(), "env")
        finally:
            os.environ.pop("WEB_PASSWORD", None)

    def test_password_file_is_mode_600(self):
        self.main.web_credentials()
        mode = stat.S_IMODE(os.stat(self.pwfile).st_mode)
        self.assertEqual(mode, 0o600)

    def test_unwritable_location_falls_back_to_memory(self):
        self.main.ACCOUNT_FILE = Path("/proc/definitely-not-here/account.json")
        _, password = self.main.web_credentials()
        self.assertTrue(password)
        self.assertEqual(self.main.web_password_source(), "memory")

    def test_generated_password_actually_authenticates(self):
        from fastapi import HTTPException
        from fastapi.security import HTTPBasicCredentials
        from starlette.requests import Request
        _, password = self.main.web_credentials()
        request = Request({"type": "http", "method": "GET", "headers": [],
                           "path": "/api/stats", "query_string": b""})
        self.main.authorized(request, HTTPBasicCredentials(username="admin", password=password))
        with self.assertRaises(HTTPException):
            self.main.authorized(
                request, HTTPBasicCredentials(username="admin", password="wrong"))


if __name__ == "__main__":
    unittest.main()
