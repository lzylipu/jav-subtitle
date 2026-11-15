"""日志文案必须是中文，别把 done / asr 这种内部值直接吐给用户。

日志页是给人看任务进度的，用户原话是「内容上尽量展示中文注释」。
"""
import unittest

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from app import db


class StageLabelTests(unittest.TestCase):
    def test_three_stages_are_chinese(self):
        self.assertEqual(db.stage_label("extract"), "抽音频")
        self.assertEqual(db.stage_label("asr"), "转写")
        self.assertEqual(db.stage_label("translate"), "翻译")

    def test_unknown_stage_falls_back_to_raw(self):
        self.assertEqual(db.stage_label("weird"), "weird")

    def test_status_labels_are_chinese(self):
        self.assertEqual(db.status_label("done"), "完成")
        self.assertEqual(db.status_label("failed"), "失败")
        self.assertEqual(db.status_label("skipped"), "跳过")

    def test_finish_with_terminal_stage_does_not_print_it(self):
        # pipeline 收尾时 finish(job_id, "done", "done", "完成")，stage 是终态不是阶段名
        rows = []
        original = db.add_event
        db.add_event = lambda job_id, message, level="info": rows.append(message)
        try:
            db.finish(1, "done", "done", "完成")
        finally:
            db.add_event = original
        self.assertEqual(rows[0], "任务完成")
        self.assertNotIn("done", rows[0])

    def test_finish_event_has_no_raw_english(self):
        rows = []
        original = db.add_event
        db.add_event = lambda job_id, message, level="info": rows.append((message, level))
        try:
            db.finish(1, "translate", "done", "完成")
            db.finish(2, "asr", "failed", "翻译接口鉴权失败 HTTP 401")
            db.finish(3, "extract", "skipped", "")
        finally:
            db.add_event = original

        self.assertEqual(len(rows), 3)
        first, level = rows[0]
        self.assertIn("翻译", first)
        self.assertNotIn("done", first)
        self.assertNotIn("translate", first)
        self.assertEqual(level, "info")

        second, level = rows[1]
        self.assertIn("失败", second)
        self.assertNotIn("failed", second)
        self.assertNotIn("asr", second)
        self.assertEqual(level, "warn")

        third, _ = rows[2]
        self.assertIn("跳过", third)
        self.assertNotIn("skipped", third)


if __name__ == "__main__":
    unittest.main()
