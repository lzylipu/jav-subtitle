"""测试环境：必须在导入 app 之前设置 DATA_DIR。"""
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="nas-sub-test-")
os.environ["DATA_DIR"] = TMP
os.environ["WEB_PASSWORD"] = "test"
os.environ["MEDIA_ROOT"] = os.path.join(TMP, "media")
os.makedirs(os.environ["MEDIA_ROOT"], exist_ok=True)


def reset() -> None:
    """清空任务/媒体/缓存，保留 schema。"""
    from app.db import connect, init_db
    init_db()
    with connect() as conn:
        for table in ("jobs", "media", "events", "tm", "counters"):
            conn.execute(f"DELETE FROM {table}")
