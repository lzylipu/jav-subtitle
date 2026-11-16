"""转写固定走 CPU 的约束：别哪天又悄悄加回 GPU 分支。

为什么要有这几条：CTranslate2 的 Python wheel 只支持 CPU / NVIDIA CUDA / AMD ROCm，
Intel 核显（N100 这类小主机就是）和 AMD 核显都不在里面。为了核显换 OpenVINO 引擎
代价太大、收益很小，所以这个项目明确只用 CPU。这几条测试把这个决定钉死。
"""
import unittest
from unittest import mock

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from app import asr


class CpuOnlyTests(unittest.TestCase):
    def test_model_is_always_created_on_cpu_with_int8(self):
        asr.clear_cache()
        created = {}

        class FakeModel:
            def __init__(self, name, device="cpu", compute_type="float32", cpu_threads=0):
                created.update(name=name, device=device, compute_type=compute_type,
                               cpu_threads=cpu_threads)

        # faster_whisper 是延迟导入的（见 app.asr._load_engine），
        # 所以这里替换 _load_engine，而不是模块属性 WhisperModel。
        with mock.patch.object(asr, "_load_engine", return_value=(FakeModel, None)):
            asr.get_model("small", 4)
        self.assertEqual(created["device"], "cpu")
        self.assertEqual(created["compute_type"], "int8")
        self.assertEqual(created["cpu_threads"], 4)
        asr.clear_cache()

    def test_auto_threads_capped_at_four(self):
        with mock.patch("os.cpu_count", return_value=16):
            self.assertEqual(asr.threads_for(0), asr.MAX_AUTO_THREADS)
        with mock.patch("os.cpu_count", return_value=2):
            self.assertEqual(asr.threads_for(0), 2)

    def test_auto_threads_never_zero(self):
        with mock.patch("os.cpu_count", return_value=None):
            self.assertEqual(asr.threads_for(0), 1)

    def test_explicit_threads_win(self):
        with mock.patch("os.cpu_count", return_value=16):
            self.assertEqual(asr.threads_for(7), 7)


class SettingsHaveNoDeviceKnobTests(unittest.TestCase):
    """设备/精度不是可调项，settings 里不该再出现这两个字段。"""

    def test_no_device_or_compute_type_field(self):
        from dataclasses import fields
        from app.config import Settings
        names = {f.name for f in fields(Settings)}
        self.assertNotIn("device", names)
        self.assertNotIn("compute_type", names)
        self.assertIn("cpu_threads", names)


if __name__ == "__main__":
    unittest.main()
