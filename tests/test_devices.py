"""设备自动识别测试：有 GPU 用 GPU，没 GPU 回退 CPU，任何探测失败都不许崩。"""
import unittest
from unittest import mock

import env  # noqa: F401  —— 必须先 import，设置临时 DATA_DIR

from app import devices


def fake_ct2(count=0, types=None, raises=None):
    module = mock.MagicMock()
    if raises is not None:
        module.get_cuda_device_count.side_effect = raises
        return module
    module.get_cuda_device_count.return_value = count
    module.get_supported_compute_types.return_value = set(types or ["float16", "int8_float16"])
    return module


class ProbeTests(unittest.TestCase):
    def test_no_gpu_gives_zero_devices(self):
        with mock.patch.dict("sys.modules", {"ctranslate2": fake_ct2(0)}), \
             mock.patch.object(devices, "_nvidia_smi", return_value=([], [])):
            info = devices.probe()
        self.assertEqual(info.cuda_device_count, 0)
        self.assertFalse(info.has_cuda)
        self.assertTrue(info.cuda_error)       # 要能说清为什么没有

    def test_gpu_detected_with_name_and_memory(self):
        with mock.patch.dict("sys.modules", {"ctranslate2": fake_ct2(1)}), \
             mock.patch.object(devices, "_nvidia_smi", return_value=(["RTX 3060"], [12288])):
            info = devices.probe()
        self.assertTrue(info.has_cuda)
        self.assertEqual(info.gpu_names, ["RTX 3060"])
        self.assertEqual(info.gpu_memory_mb, [12288])

    def test_probe_never_raises_when_ct2_broken(self):
        with mock.patch.dict("sys.modules",
                             {"ctranslate2": fake_ct2(raises=RuntimeError("no driver"))}):
            info = devices.probe()
        self.assertFalse(info.has_cuda)
        self.assertIn("no driver", info.cuda_error)


class ResolveTests(unittest.TestCase):
    def _resolve(self, count, mem=None, device="auto", compute="auto", threads=None):
        with mock.patch.dict("sys.modules",
                             {"ctranslate2": fake_ct2(count, ["float16", "int8_float16"])}), \
             mock.patch.object(devices, "_nvidia_smi",
                               return_value=(["NVIDIA GTX 1650"], mem or [4096])):
            return devices.resolve(device, compute, threads)

    def test_auto_picks_cuda_when_gpu_present(self):
        plan = self._resolve(1)
        self.assertEqual(plan.device, "cuda")
        self.assertEqual(plan.compute_type, "float16")
        self.assertEqual(plan.cpu_threads, 1)      # GPU 时不需要多线程喂数据
        self.assertIn("自动识别", plan.reason)

    def test_auto_falls_back_to_cpu_without_gpu(self):
        plan = self._resolve(0)
        self.assertEqual(plan.device, "cpu")
        self.assertEqual(plan.compute_type, "int8")
        self.assertGreaterEqual(plan.cpu_threads, 1)
        self.assertIn("CPU", plan.reason)

    def test_low_vram_switches_to_int8_float16(self):
        plan = self._resolve(1, mem=[2048])
        self.assertEqual(plan.device, "cuda")
        self.assertEqual(plan.compute_type, "int8_float16")

    def test_forced_cuda_without_gpu_downgrades(self):
        plan = self._resolve(0, device="cuda")
        self.assertEqual(plan.device, "cpu")
        self.assertIn("回退", plan.reason)

    def test_forced_cpu_wins_over_gpu(self):
        plan = self._resolve(1, device="cpu")
        self.assertEqual(plan.device, "cpu")
        self.assertEqual(plan.compute_type, "int8")

    def test_explicit_compute_type_is_respected(self):
        plan = self._resolve(0, compute="float32")
        self.assertEqual(plan.compute_type, "float32")

    def test_explicit_threads_win(self):
        plan = self._resolve(0, threads=7)
        self.assertEqual(plan.cpu_threads, 7)

    def test_garbage_settings_fall_back_to_auto(self):
        plan = self._resolve(0, device="tpu", compute="fp4")
        self.assertEqual(plan.requested_device, "auto")
        self.assertEqual(plan.requested_compute, "auto")
        self.assertEqual(plan.device, "cpu")

    def test_plan_is_json_safe(self):
        import json
        plan = self._resolve(1)
        json.dumps(plan.to_dict())      # 不能有 set / 对象漏进去


class AsrDeviceWiringTests(unittest.TestCase):
    def test_model_is_created_on_requested_device(self):
        """get_model 必须把 device 传下去，不能自己写死 cpu。"""
        from app import asr
        asr.clear_cache()
        created = {}

        class FakeModel:
            def __init__(self, name, device="cpu", compute_type="float32", cpu_threads=0):
                created.update(name=name, device=device, compute_type=compute_type,
                               cpu_threads=cpu_threads)

        with mock.patch.object(asr, "WhisperModel", FakeModel), \
             mock.patch.object(asr, "BatchedInferencePipeline", None):
            asr.get_model("small", "cuda", "float16", 1)
        self.assertEqual(created["device"], "cuda")
        self.assertEqual(created["compute_type"], "float16")
        asr.clear_cache()


class DefaultDeviceSettingTests(unittest.TestCase):
    def test_default_is_auto(self):
        from app.config import Settings
        s = Settings()
        self.assertEqual(s.device, "auto")
        self.assertEqual(s.compute_type, "auto")
        self.assertEqual(s.cpu_threads, 0)


if __name__ == "__main__":
    unittest.main()
