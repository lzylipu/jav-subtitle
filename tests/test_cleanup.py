"""清理功能：用户卡在「跑坏了想重来但没地方删」。

重点锁三件事：
1. 原视频永远删不掉（哪怕有人把字幕勾上了）；
2. 作用范围逃不出媒体根目录；
3. 没带 confirm 不许执行。
"""
import os
import unittest
from pathlib import Path

from tests import env

from app import cleanup
from app.db import connect, init_db


def touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class CleanupTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        self.root = Path(env.os.environ["MEDIA_ROOT"])
        self.movie = self.root / "JULIA" / "PPPD-424"
        touch(self.movie / "PPPD-424.mp4", b"video" * 100)
        touch(self.movie / "PPPD-424.ja.srt", b"japanese subtitle")
        touch(self.movie / "PPPD-424.zh-CN.srt", b"chinese subtitle")
        touch(self.movie / "PPPD-424.en.srt", b"english")     # 不是我们生成的
        other = self.root / "其他" / "X.mp4"
        touch(other, b"video")

        with connect() as conn:
            for p in (self.movie / "PPPD-424.mp4", self.root / "其他" / "X.mp4"):
                conn.execute(
                    "INSERT INTO media(path,size,mtime,discovered_at,updated_at) "
                    "VALUES(?,?,?,?,?)", (str(p), 1, 1.0, "t", "t"))
            job_id = conn.execute(
                "INSERT INTO jobs(video_path,stage,status,created_at) VALUES(?,?,?,?)",
                (str(self.movie / "PPPD-424.mp4"), "translate", "done", "t")).lastrowid
            conn.execute(
                "INSERT INTO jobs(video_path,stage,status,created_at) VALUES(?,?,?,?)",
                (str(self.root / "其他" / "X.mp4"), "asr", "failed", "t"))
            conn.execute("INSERT INTO tm(key,source,target,created_at) VALUES(?,?,?,?)",
                         ("k", "あ", "啊", "t"))
            # job_id 用真实自增 id，否则测试断言会因为 id 不匹配而假失败
            conn.execute("INSERT INTO events(job_id,created_at,level,message) VALUES(?,?,?,?)",
                         (job_id, "t", "info", "hi"))

    def test_preview_counts_without_deleting(self):
        r = cleanup.preview(str(self.root))
        self.assertEqual(r["items"]["jobs"]["count"], 2)
        self.assertEqual(r["items"]["index"]["count"], 2)
        self.assertEqual(r["items"]["subtitles"]["count"], 2)   # 只数 .ja.srt 和 .zh-CN.srt
        # 预览不能动硬盘
        self.assertTrue((self.movie / "PPPD-424.zh-CN.srt").exists())
        self.assertTrue((self.movie / "PPPD-424.mp4").exists())

    def test_preview_respects_scope(self):
        r = cleanup.preview(str(self.root), str(self.movie))
        self.assertEqual(r["items"]["jobs"]["count"], 1)
        self.assertEqual(r["items"]["index"]["count"], 1)
        self.assertEqual(r["items"]["subtitles"]["count"], 2)

    def test_clean_never_deletes_video_or_foreign_subtitles(self):
        cleanup.clean(str(self.root), "", "zh-CN", ["subtitles"])
        self.assertTrue((self.movie / "PPPD-424.mp4").exists(), "原视频被删了！")
        self.assertFalse((self.movie / "PPPD-424.ja.srt").exists())
        self.assertFalse((self.movie / "PPPD-424.zh-CN.srt").exists())
        self.assertTrue((self.movie / "PPPD-424.en.srt").exists(),
                        "不认识的 .srt 不该动")

    def test_clean_jobs_also_removes_events(self):
        cleanup.clean(str(self.root), "", "zh-CN", ["jobs"])
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            # job_id > 0 的事件都该跟着走；job_id=0 是全局日志（清理本身也会写一条），留着
            left = conn.execute("SELECT COUNT(*) FROM events WHERE job_id > 0").fetchone()[0]
            self.assertEqual(left, 0)

    def test_clean_scope_limits_jobs(self):
        cleanup.clean(str(self.root), str(self.movie), "zh-CN", ["jobs"])
        with connect() as conn:
            rows = conn.execute("SELECT video_path FROM jobs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("其他", rows[0][0])

    def test_clean_index_and_tm(self):
        r = cleanup.clean(str(self.root), "", "zh-CN", ["index", "tm"])
        self.assertEqual(r["items"]["index"], 2)
        self.assertEqual(r["items"]["tm"], 1)
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

    def test_scope_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            cleanup.preview(str(self.root), "/etc")
        with self.assertRaises(ValueError):
            cleanup.clean(str(self.root), "/tmp", "zh-CN", ["jobs"])

    def test_unknown_items_are_ignored(self):
        r = cleanup.clean(str(self.root), "", "zh-CN", ["nonsense"])
        self.assertEqual(r["items"], {})

    def test_disk_usage_reports_paths(self):
        usage = cleanup.disk_usage()
        self.assertIn("data", usage)
        self.assertIn("work", usage)
        self.assertTrue(Path(usage["data"]["path"]).exists())


class CleanApiTests(unittest.TestCase):
    """接口层：没有 confirm 不许真删。"""

    def setUp(self):
        env.reset()
        import app.main  # noqa: F401  确保模块可导入（副作用：建库、读口令）

    def test_api_requires_confirm(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, headers={"Authorization": "Basic YWRtaW46dGVzdA=="})
        with client:
            r = client.post("/api/clean", json={"items": ["tm"], "confirm": False})
            self.assertEqual(r.status_code, 400)
            r = client.post("/api/clean", json={"items": [], "confirm": True})
            self.assertEqual(r.status_code, 400)
            r = client.post("/api/clean", json={"items": ["bogus"], "confirm": True})
            self.assertEqual(r.status_code, 400)

    def test_preview_endpoint(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, headers={"Authorization": "Basic YWRtaW46dGVzdA=="})
        with client:
            r = client.get("/api/clean/preview?items=jobs,index")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("items", body)
            self.assertIn("scope", body)


if __name__ == "__main__":
    unittest.main()
