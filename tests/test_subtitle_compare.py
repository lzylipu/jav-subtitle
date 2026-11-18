"""字幕时间轴对比工具的回归测试。

只测纯函数，不碰网络、不读真实字幕文件。
"""
import unittest

from tools.subtitle_compare import Cue, compare, describe, parse_srt, start_offsets, text_style


class ParseTests(unittest.TestCase):
    def test_parses_basic_srt(self):
        srt = ("1\n00:00:01,000 --> 00:00:03,500\nこんにちは\n\n"
               "2\n00:01:02,250 --> 00:01:04,000\n世界\n")
        cues = parse_srt_file(srt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[0].end, 3.5)
        self.assertAlmostEqual(cues[1].start, 62.25)

    def test_skips_broken_block_without_failing(self):
        srt = ("1\n坏时间轴\nx\n\n"
               "2\n00:00:05,000 --> 00:00:06,000\nok\n")
        cues = parse_srt_file(srt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "ok")


def parse_srt_file(content: str):
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile("w", suffix=".srt", encoding="utf-8", delete=False) as fh:
        fh.write(content)
        name = fh.name
    try:
        return parse_srt(Path(name))
    finally:
        Path(name).unlink(missing_ok=True)


class DescribeTests(unittest.TestCase):
    def test_coverage_and_long_cues(self):
        cues = [Cue(0.0, 5.0, "a"), Cue(6.0, 9.0, "b"), Cue(10.0, 30.0, "c")]
        d = describe(cues)
        self.assertEqual(d["cues"], 3)
        self.assertEqual(d["span_s"], 30.0)
        self.assertAlmostEqual(d["speech_s"], 28.0)
        self.assertAlmostEqual(d["coverage"], 0.9333, places=3)
        self.assertEqual(d["over_8s"], 1)
        self.assertEqual(d["over_12s"], 1)
        self.assertEqual(d["gap_median_s"], 1.0)
        self.assertEqual(d["chars_median"], 1.0)
        self.assertAlmostEqual(d["chars_per_speech_s"], 3 / 28, places=2)

    def test_gap_and_overlap_detection(self):
        cues = [Cue(0.0, 2.0, "a"), Cue(5.0, 7.0, "b"), Cue(6.5, 8.0, "c")]
        d = describe(cues)
        self.assertEqual(d["gaps_gt_2s"], 1)
        self.assertEqual(d["gap_seconds_gt_2s"], 3.0)
        self.assertEqual(d["overlaps"], 1)


class AlignmentTests(unittest.TestCase):
    def test_identical_timelines_have_zero_offset(self):
        cues = [Cue(0.0, 2.0, "a"), Cue(3.0, 5.0, "b")]
        offsets = start_offsets(cues, cues)
        self.assertEqual(offsets, [0.0, 0.0])

    def test_constant_delay_is_detected(self):
        ref = [Cue(0.0, 2.0, "a"), Cue(3.0, 5.0, "b")]
        out = [Cue(0.5, 2.5, "a"), Cue(3.5, 5.5, "b")]
        report = compare(ref, out)
        self.assertAlmostEqual(report["alignment"]["start_offset_median_s"], 0.5)
        self.assertAlmostEqual(report["alignment"]["within_1s_ratio"], 1.0)

    def test_missing_tail_shows_fewer_pairs(self):
        ref = [Cue(0.0, 2.0, "a"), Cue(3.0, 5.0, "b"), Cue(6.0, 8.0, "c")]
        out = [Cue(0.0, 2.0, "a")]
        report = compare(ref, out)
        self.assertLess(report["alignment"]["paired"], len(ref))
        self.assertEqual(report["produced"]["cues"], 1)


class StyleTests(unittest.TestCase):
    def test_reports_line_and_leading_conventions(self):
        cues = [Cue(1.0, 3.0, "第一行\n第二行"), Cue(5.0, 7.0, "短句")]
        style = text_style(cues)
        self.assertEqual(style["text_lines"], 3)
        self.assertEqual(style["lines_per_cue_median"], 1)
        self.assertEqual(style["line_chars_median"], 3)
        self.assertEqual(style["line_columns_median"], 6)
        self.assertEqual(style["max_line_columns"], 6)
        self.assertEqual(style["leading_gap_s"], 1.0)

    def test_empty_cues_have_safe_style_values(self):
        style = text_style([])
        self.assertEqual(style["text_lines"], 0)
        self.assertEqual(style["max_line_chars"], 0)
        self.assertEqual(style["max_line_columns"], 0)


if __name__ == "__main__":
    unittest.main()
