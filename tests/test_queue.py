import shutil
import tempfile
import unittest
from pathlib import Path

import env

from app import asr, pipeline
from app.config import Settings
from app.db import connect, load_settings, save_settings, update_job
from app.scanner import scan_media
from app.subtitles import Cue, render_srt, target_paths


class QueueTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = load_settings()
        save_settings(Settings(media_root=str(self.tmp), min_file_age=0, target_language="zh-CN"))

    def tearDown(self):
        save_settings(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _video(self, name="v.mp4"):
        path = self.tmp / name
        path.write_bytes(b"fake")
        return path

    def test_queue_requires_two_scans(self):
        video = self._video()
        scan_media(str(self.tmp))
        self.assertEqual(pipeline.queue_indexed(), 0, "第一次扫描只是建立基线")
        scan_media(str(self.tmp))
        self.assertEqual(pipeline.queue_indexed(), 1)
        self.assertEqual(pipeline.queue_indexed(), 0, "已有任务不重复入队")
        with connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE video_path=?", (str(video),)).fetchone()
        self.assertEqual(row["stage"], "extract")
        self.assertEqual(row["status"], "pending")

    def test_existing_chinese_srt_is_skipped_not_queued(self):
        video = self._video("done.mp4")
        zh, _ = target_paths(video, "zh-CN")
        zh.write_text("1\n00:00:00,000 --> 00:00:01,000\n已有\n", encoding="utf-8")
        scan_media(str(self.tmp))
        scan_media(str(self.tmp))
        self.assertEqual(pipeline.queue_indexed(), 0)
        with connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM jobs WHERE video_path=?", (str(video),)).fetchone())

    def test_ffmpeg_extracts_mono_16k(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("本机没有 ffmpeg")
        import subprocess
        video = self.tmp / "tone.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "aac", str(video)], check=True)
        out = self.tmp / "out.wav"
        asr.extract_audio(video, out)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "stream=channels,sample_rate", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(probe, "16000,1")

    def test_broken_video_says_why_instead_of_ffmpeg_dump(self):
        """坏文件／假视频要给中文原因，不许把 ffmpeg 命令行 + returncode 183 抛给用户"""
        if not shutil.which("ffmpeg"):
            self.skipTest("本机没有 ffmpeg")
        fake = self.tmp / "broken.mp4"
        fake.write_bytes(b"\0" * (2 * 1024 * 1024))
        with self.assertRaises(ValueError) as ctx:
            asr.extract_audio(fake, self.tmp / "broken.wav")
        msg = str(ctx.exception)
        self.assertIn("视频解码失败", msg)
        self.assertNotIn("CalledProcessError", msg)
        self.assertNotIn("-hide_banner", msg)

    def test_stability_gate_survives_rescan(self):
        """回归：多次扫描不能刷新 updated_at，否则稳定闸门永远不通过。"""
        env_reset_settings = Settings(media_root=str(self.tmp), min_file_age=1, target_language="zh-CN")
        save_settings(env_reset_settings)
        self._video("gate.mp4")
        import time as _t
        scan_media(str(self.tmp))
        _t.sleep(1.2)
        scan_media(str(self.tmp))
        self.assertEqual(pipeline.queue_indexed(), 1, "文件已稳定，应能入队")

    def test_priority_ordering(self):
        for i in range(3):
            (self.tmp / f"v{i}.mp4").write_bytes(b"x")
        scan_media(str(self.tmp))
        scan_media(str(self.tmp))
        pipeline.queue_indexed()
        with connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM jobs ORDER BY id").fetchall()]
            conn.execute("UPDATE jobs SET priority=10 WHERE id=?", (ids[-1],))
        claimed = pipeline.claim("extract")
        self.assertEqual(claimed["id"], ids[-1], "高优先级先出队")


if __name__ == "__main__":
    unittest.main()
