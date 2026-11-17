"""音轨增益 + VAD 阶梯：修「人声漏识别」的回归测试。

真机根因（10.10.0.8 / IPZZ-763 / v3.10.1 镜像）：
- 片源平均音量 -29.4dB，Silero VAD 的能量判据把大段对白判成非语音；
- 整片 116.9 分钟只认出 94 条字幕，占用率 2.1%，而真人日文字幕健康区间
  是 35%~80%；
- 修复手段是「抽音频后测平均音量 → 太轻就定增益」+「VAD 阈值阶梯退让」。

这些用例不跑模型，只锁住决策逻辑：什么时候增益、增益多少、VAD 怎么退。
"""
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests import env  # noqa: F401  必须在导入 app 之前设置 DATA_DIR

from app import asr


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class AudioLevelTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="asr-gain-test-"))
        self.audio = self.tmp / "a.wav"
        self.audio.write_bytes(b"RIFFfake")

    def _fake_ffmpeg(self, returncode=0, stderr=""):
        """模拟 ffmpeg：成功时真的产出目标文件，失败时什么都不产出。"""
        def run(cmd, **kwargs):
            if returncode == 0:
                Path(cmd[-1]).write_bytes(b"RIFFboosted")
            return FakeCompleted(returncode=returncode, stderr=stderr)
        return run

    def test_mean_db_parsed_from_ffmpeg_output(self):
        stderr = ("[Parsed_volumedetect_0 @ 0x1] mean_volume: -29.4 dB\n"
                  "[Parsed_volumedetect_0 @ 0x1] max_volume: -0.7 dB\n")
        with mock.patch.object(asr.subprocess, "run",
                               return_value=FakeCompleted(stderr=stderr)):
            self.assertAlmostEqual(asr.audio_mean_db(Path("/x.wav")), -29.4)

    def test_mean_db_returns_zero_when_unavailable(self):
        with mock.patch.object(asr.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(asr.audio_mean_db(Path("/x.wav")), 0.0)

    def test_normal_level_audio_is_not_boosted(self):
        with mock.patch.object(asr, "audio_mean_db", return_value=-18.0):
            gain, changed = asr.normalize_audio(self.audio)
        self.assertFalse(changed)
        self.assertEqual(gain, 0.0)

    def test_quiet_audio_gets_fixed_gain(self):
        with mock.patch.object(asr, "audio_mean_db", return_value=-29.4), \
             mock.patch.object(asr.subprocess, "run", side_effect=self._fake_ffmpeg()):
            gain, changed = asr.normalize_audio(self.audio)
        self.assertTrue(changed)
        self.assertAlmostEqual(gain, 9.4, places=1)
        self.assertEqual(self.audio.read_bytes(), b"RIFFboosted")

    def test_gain_is_capped(self):
        with mock.patch.object(asr, "audio_mean_db", return_value=-60.0), \
             mock.patch.object(asr.subprocess, "run", side_effect=self._fake_ffmpeg()):
            gain, changed = asr.normalize_audio(self.audio)
        self.assertTrue(changed)
        self.assertLessEqual(gain, asr.AUDIO_MAX_GAIN_DB)

    def test_failed_boost_raises_clear_error(self):
        with mock.patch.object(asr, "audio_mean_db", return_value=-29.4), \
             mock.patch.object(asr.subprocess, "run",
                               side_effect=self._fake_ffmpeg(returncode=1,
                                                            stderr="bad input")):
            with self.assertRaises(ValueError):
                asr.normalize_audio(self.audio)


class VadLadderTests(unittest.TestCase):
    """VAD 阶梯：覆盖率不达标就逐级放宽，取覆盖率最高的一档。"""

    def _fake_collect(self, coverages):
        calls = []

        def collect(model, batched, audio, duration, ranges, kwargs, batch_size, progress):
            vad = kwargs.get("vad_parameters")
            calls.append(vad["threshold"] if vad else "off")
            level = min(len(calls) - 1, len(coverages) - 1)
            coverage = coverages[level]
            words = [{"start": 0.0, "end": duration * coverage,
                      "text": "あ", "prob": 0.9}]
            return words, "ja"
        return collect, calls

    def test_strict_level_sufficient_stops_immediately(self):
        collect, calls = self._fake_collect([0.5])
        with mock.patch.object(asr, "_collect_windowed", collect):
            words, lang = asr._vad_ladder(None, None, Path("/x.wav"), 100.0,
                                          [(0.0, 100.0)], {}, 1, None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(lang, "ja")
        self.assertEqual(len(words), 1)

    def test_ladder_escalates_until_coverage_is_good(self):
        collect, calls = self._fake_collect([0.01, 0.05, 0.4])
        with mock.patch.object(asr, "_collect_windowed", collect):
            words, _lang = asr._vad_ladder(None, None, Path("/x.wav"), 100.0,
                                           [(0.0, 100.0)], {}, 1, None)
        self.assertEqual(calls, [0.5, 0.2, 0.05])
        self.assertAlmostEqual(words[0]["end"], 40.0)

    def test_ladder_falls_back_to_disabling_vad(self):
        collect, calls = self._fake_collect([0.01, 0.02, 0.03, 0.5])
        with mock.patch.object(asr, "_collect_windowed", collect):
            words, _lang = asr._vad_ladder(None, None, Path("/x.wav"), 100.0,
                                           [(0.0, 100.0)], {}, 1, None)
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(words[0]["end"], 50.0)

    def test_ladder_keeps_best_level_when_all_are_bad(self):
        collect, calls = self._fake_collect([0.01, 0.03, 0.02, 0.02])
        with mock.patch.object(asr, "_collect_windowed", collect):
            words, _lang = asr._vad_ladder(None, None, Path("/x.wav"), 100.0,
                                           [(0.0, 100.0)], {}, 1, None)
        self.assertEqual(len(calls), 4)
        # 四档都低于下限，取覆盖率最高的第二档
        self.assertAlmostEqual(words[0]["end"], 3.0)


class WindowRangeTests(unittest.TestCase):
    def test_short_audio_is_one_window(self):
        self.assertEqual(asr.window_ranges(300.0), [(0.0, 300.0)])

    def test_long_audio_is_windowed_with_overlap(self):
        ranges = asr.window_ranges(2000.0)
        self.assertGreater(len(ranges), 1)
        self.assertEqual(ranges[0][0], 0.0)
        step = ranges[1][0] - ranges[0][0]
        self.assertAlmostEqual(step, asr.ASR_WINDOW_SECONDS - asr.ASR_WINDOW_OVERLAP_SECONDS)
        self.assertAlmostEqual(ranges[-1][0] + ranges[-1][1], 2000.0, places=3)


if __name__ == "__main__":
    unittest.main()
