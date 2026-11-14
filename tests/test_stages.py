import shutil
import tempfile
import unittest
from pathlib import Path

import env

from app import pipeline
from app.config import Settings
from app.db import connect, counter_add, load_settings, save_settings, today
from app.subtitles import Cue, parse_srt, render_srt, target_paths


class FakeEngine:
    """记录调用次数的假翻译器。"""
    def __init__(self):
        self.calls = []

    def translate(self, cues):
        self.calls.append([c.text for c in cues])
        return ["中:" + c.text for c in cues]


class StageTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = load_settings()
        self.settings = Settings(media_root=str(self.tmp), min_file_age=0,
                                 openai_model="m", translation_batch_size=2,
                                 daily_char_budget=0, tm_enabled=True, keep_japanese_srt=True)
        save_settings(self.settings)
        self.video = self.tmp / "ep1.mp4"
        self.video.write_bytes(b"fake")
        with connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id,video_path,stage,status,created_at) VALUES(1,?,'extract','pending','x')",
                (str(self.video),))

    def tearDown(self):
        save_settings(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _job(self):
        with connect() as conn:
            return dict(conn.execute("SELECT * FROM jobs WHERE id=1").fetchone())

    def test_extract_reuses_existing_japanese_srt(self):
        zh, ja = target_paths(self.video, "zh-CN")
        ja.write_text(render_srt([Cue(1, "00:00:00,000", "00:00:01,000", "おはよう")]), encoding="utf-8")
        pipeline.handle_extract(self._job(), load_settings())
        job = self._job()
        self.assertEqual(job["stage"], "translate")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["ja_srt"], str(ja))

    def test_translate_writes_srt_and_uses_memory(self):
        _, ja = target_paths(self.video, "zh-CN")
        cues = [Cue(1, "00:00:00,000", "00:00:01,000", "おはよう"),
                Cue(2, "00:00:02,000", "00:00:03,000", "こんばんは")]
        ja.write_text(render_srt(cues), encoding="utf-8")
        with connect() as conn:
            conn.execute("UPDATE jobs SET stage='translate',ja_srt=? WHERE id=1", (str(ja),))

        engine = FakeEngine()
        original = pipeline.translator
        pipeline.translator = lambda settings: engine   # noqa: ARG005
        try:
            pipeline.handle_translate(self._job(), load_settings())
            job = self._job()
            self.assertEqual(job["status"], "done")
            self.assertEqual(len(engine.calls), 1, "两条一批，一次调用")

            zh, _ = target_paths(self.video, "zh-CN")
            self.assertTrue(zh.is_file())
            self.assertEqual([c.text for c in parse_srt(zh.read_text())], ["中:おはよう", "中:こんばんは"])

            # 同一批台词再翻一次应全部命中翻译记忆，不再请求 API
            zh.unlink()
            with connect() as conn:
                conn.execute("UPDATE jobs SET status='pending',stage='translate' WHERE id=1")
            pipeline.handle_translate(self._job(), load_settings())
            self.assertEqual(len(engine.calls), 1, "缓存命中不应再调用翻译接口")
        finally:
            pipeline.translator = original

    def test_budget_blocks_translation(self):
        _, ja = target_paths(self.video, "zh-CN")
        ja.write_text(render_srt([Cue(1, "00:00:00,000", "00:00:01,000", "長い台詞です")]), encoding="utf-8")
        with connect() as conn:
            conn.execute("UPDATE jobs SET stage='translate',ja_srt=? WHERE id=1", (str(ja),))
        save_settings(Settings(**{**self.settings.__dict__, "daily_char_budget": 5}))
        counter_add(f"chars:{today()}", 100)
        engine = FakeEngine()
        original = pipeline.translator
        pipeline.translator = lambda settings: engine   # noqa: ARG005
        try:
            with self.assertRaises(pipeline.BudgetExceeded):
                pipeline.handle_translate(self._job(), load_settings())
            self.assertEqual(engine.calls, [], "预算耗尽时一次都不该请求")
            self.assertGreater(pipeline.budget_block_until["ts"], 0)
        finally:
            pipeline.translator = original
            pipeline.budget_block_until["ts"] = 0.0

    def test_missing_audio_falls_back_to_extract(self):
        with connect() as conn:
            conn.execute("UPDATE jobs SET stage='asr',audio_path='/nope/x.wav' WHERE id=1")
        pipeline.handle_asr(self._job(), load_settings())
        self.assertEqual(self._job()["stage"], "extract")


if __name__ == "__main__":
    unittest.main()
