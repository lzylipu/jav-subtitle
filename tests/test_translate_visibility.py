"""「卡在 70% 看不出在干什么」的回归测试。

翻译引擎内部要重试、拆批、逐条抢救，一卡就是几分钟；这期间任务表和状态栏
都不动，用户只会说「卡住了」。所以要保证两条：

1. 一批翻译失败时，事件表里留下批次范围和真实原因；
2. /api/stats 带着最近一次翻译错误，前端才有东西可显示。
"""
import unittest

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from app import pipeline


class TranslateVisibilityTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        pipeline.last_translate_error["text"] = ""
        pipeline.last_translate_error["ts"] = 0.0

    def test_stats_exposes_last_translate_error(self):
        import time as _t
        pipeline.last_translate_error["text"] = "HTTP 401 无效的令牌"
        pipeline.last_translate_error["ts"] = _t.time()
        snap = pipeline.stats()["translate_api"]
        self.assertEqual(snap["last_error"], "HTTP 401 无效的令牌")
        self.assertGreaterEqual(snap["last_error_age"], 0)

    def test_stats_without_error_reports_minus_one(self):
        snap = pipeline.stats()["translate_api"]
        self.assertEqual(snap["last_error"], "")
        self.assertEqual(snap["last_error_age"], -1)


if __name__ == "__main__":
    unittest.main()
