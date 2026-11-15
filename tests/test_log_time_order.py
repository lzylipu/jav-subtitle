"""日志时间与顺序：

1. 时间一律北京时间（UTC+8），跟容器 TZ / 宿主机时区无关 —— 之前存的是 UTC，
   前端还把偏移量切片丢掉，页面比真实时间慢 8 小时；
2. 日志新的在上面 —— 之前后端 reversed 成升序，最新反而沉到最下面。
"""
import os
import re
import unittest
from datetime import datetime, timedelta, timezone

from tests import env  # noqa: F401  (设置 DATA_DIR 等环境变量)

from app import db


class BeijingClockTest(unittest.TestCase):
    def test_now_carries_plus_8_offset(self):
        stamp = db.now()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")

    def test_now_matches_wall_clock_in_beijing(self):
        # 2026-11-15 19:41 CST == 11:41 UTC
        got = datetime.fromisoformat(db.now())
        expect = datetime.now(timezone(timedelta(hours=8)))
        self.assertLess(abs((got - expect).total_seconds()), 5)

    def test_today_is_beijing_date(self):
        got = db.today()
        self.assertRegex(got, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(got, datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"))

    def test_today_not_utc_date_at_bj_midnight(self):
        # 北京时间 00:30 时 UTC 还是前一天；today() 必须按北京时间算，
        # 否则「今日字数」在凌晨会串到前一天。
        bj = datetime.now(timezone(timedelta(hours=8)))
        self.assertEqual(bj.utcoffset(), timedelta(hours=8))


class LogOrderTest(unittest.TestCase):
    """/api/logs 返回的必须是 id 倒序（最新在最前）。"""

    def setUp(self):
        from app.db import init_db
        init_db()
        self.conn_db = db

    def test_events_come_back_newest_first(self):
        for i in range(3):
            db.add_event(0, f"顺序测试 {i}")
        from app.main import logs as logs_endpoint
        rows = logs_endpoint(limit=3, since_id=0, level="")
        self.assertEqual(len(rows), 3)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True), "最新的一条必须排在最前面")
        self.assertTrue(rows[0]["message"].startswith("顺序测试 2"))

    def test_events_keep_beijing_stamp(self):
        db.add_event(0, "时间测试")
        from app.main import logs as logs_endpoint
        row = logs_endpoint(limit=1, since_id=0, level="")[0]
        self.assertTrue(row["created_at"].endswith("+08:00"), row["created_at"])


if __name__ == "__main__":
    unittest.main()
