"""字幕排版与停留时间回归测试。

对照参考字幕（NPS-476：日语两行、每行约 20 字；相邻条大多首尾相接）
锁住两个行为：
1. 日语长句折成两行，行宽不超过约 20 个全角字（40 显示列）；
2. 字幕结尾最多向静音区补 1.5 秒，不越过下一条、不超过 8 秒。
"""
import unittest

from tests import env  # noqa: F401  必须在导入 app 之前设置 DATA_DIR

from app import asr
from app.subtitles import Cue, display_width, format_cues, render_srt, wrap_text


class WrapTextTests(unittest.TestCase):
    def test_short_line_is_unchanged(self):
        self.assertEqual(wrap_text("短い"), "短い")

    def test_japanese_40_chars_becomes_two_20_char_lines(self):
        text = "あ" * 40
        wrapped = wrap_text(text)
        lines = wrapped.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual([len(line) for line in lines], [20, 20])
        self.assertLessEqual(max(display_width(line) for line in lines), 40)

    def test_42_chars_still_fits_two_lines_without_loss(self):
        text = "あ" * 42
        wrapped = wrap_text(text)
        lines = wrapped.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), text)
        self.assertLessEqual(max(display_width(line) for line in lines), 42)

    def test_break_prefers_punctuation(self):
        text = "これはテストです。つぎのぶんです。さらに続く長い台詞があります。"
        wrapped = wrap_text(text)
        first = wrapped.split("\n")[0]
        self.assertTrue(first.endswith("。"), first)

    def test_never_starts_a_line_with_punctuation(self):
        # 目标断点恰好落在「。」前面时，必须把「。」留在上一行
        text = "あ" * 19 + "。" + "い" * 20
        lines = wrap_text(text).split("\n")
        self.assertFalse(lines[1].startswith("。"), lines)

    def test_latin_prefers_space(self):
        text = "hello world this is a long subtitle line that should wrap at a space"
        lines = wrap_text(text).split("\n")
        self.assertEqual(len(lines), 2)
        self.assertFalse(lines[0].endswith(" "), repr(lines[0]))
        self.assertFalse(lines[1].startswith(" "), repr(lines[1]))
        # 断点必须落在空格处，不能把单词劈开
        self.assertRegex(lines[0], r"[A-Za-z]$", lines)
        self.assertRegex(lines[1], r"^[A-Za-z]", lines)
        self.assertEqual(" ".join([lines[0].strip(), lines[1].strip()]),
                         "hello world this is a long subtitle line that should wrap at a space")

    def test_existing_newlines_are_preserved(self):
        self.assertEqual(wrap_text("第一行\n第二行"), "第一行\n第二行")

    def test_never_loses_text(self):
        text = "これはとても長い台詞で、二行に収まらない場合でも文字を落としてはならない。"
        wrapped = wrap_text(text)
        self.assertEqual(wrapped.replace("\n", ""), text)

    def test_display_width_counts_full_width_as_two(self):
        self.assertEqual(display_width("あa"), 3)
        self.assertEqual(display_width("Ａ"), 2)


class FormatCuesTests(unittest.TestCase):
    def test_only_text_changes(self):
        cues = [Cue(1, "00:00:01,000", "00:00:03,000", "あ" * 40)]
        formatted = format_cues(cues)
        self.assertEqual(formatted[0].number, 1)
        self.assertEqual(formatted[0].start, "00:00:01,000")
        self.assertEqual(formatted[0].end, "00:00:03,000")
        self.assertIn("\n", formatted[0].text)

    def test_rendered_srt_keeps_two_lines(self):
        cues = format_cues([Cue(1, "00:00:01,000", "00:00:03,000", "あ" * 40)])
        rendered = render_srt(cues)
        self.assertIn("あ" * 20 + "\n" + "あ" * 20, rendered)


class PadCueTailsTests(unittest.TestCase):
    def test_small_gap_is_filled_without_touching_next_start(self):
        cues = [{"start": 0.0, "end": 2.0, "text": "a"},
                {"start": 3.0, "end": 4.0, "text": "b"}]
        out = asr.pad_cue_tails(cues, max_pad=1.5)
        self.assertAlmostEqual(out[0]["end"], 2.96)
        self.assertLess(out[0]["end"], out[1]["start"])

    def test_gap_larger_than_pad_keeps_a_gap(self):
        cues = [{"start": 0.0, "end": 2.0, "text": "a"},
                {"start": 10.0, "end": 11.0, "text": "b"}]
        out = asr.pad_cue_tails(cues, max_pad=1.5)
        self.assertAlmostEqual(out[0]["end"], 3.5)
        self.assertGreater(out[1]["start"] - out[0]["end"], 6.0)

    def test_never_exceeds_eight_seconds(self):
        cues = [{"start": 0.0, "end": 7.8, "text": "a"},
                {"start": 20.0, "end": 21.0, "text": "b"}]
        out = asr.pad_cue_tails(cues, max_pad=1.5, max_seconds=8.0)
        self.assertLessEqual(out[0]["end"] - out[0]["start"], 8.0)

    def test_last_cue_gets_a_small_tail(self):
        cues = [{"start": 0.0, "end": 2.0, "text": "a"}]
        out = asr.pad_cue_tails(cues, max_pad=1.5)
        self.assertAlmostEqual(out[0]["end"], 3.5)

    def test_never_shortens_a_cue(self):
        cues = [{"start": 0.0, "end": 5.0, "text": "a"},
                {"start": 5.5, "end": 6.0, "text": "b"}]
        out = asr.pad_cue_tails(cues, max_pad=1.5)
        self.assertGreaterEqual(out[0]["end"], 5.0)

    def test_does_not_mutate_input(self):
        cues = [{"start": 0.0, "end": 2.0, "text": "a"}]
        asr.pad_cue_tails(cues)
        self.assertEqual(cues[0]["end"], 2.0)


if __name__ == "__main__":
    unittest.main()


class ClampTests(unittest.TestCase):
    def test_word_timestamp_beyond_window_is_dropped(self):
        class W:
            word, probability = "あ", 0.9

        class S:
            words = [W()]

        W.start, W.end = 10.0, 11.0
        self.assertEqual(asr._collect_words([S()], 0.0, limit=5.0), [])
        W.start, W.end = 3.0, 10.0
        kept = asr._collect_words([S()], 0.0, limit=5.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["end"], 5.0)

    def test_cues_beyond_duration_are_dropped_and_clipped(self):
        cues = [
            {"start": 1.0, "end": 2.0, "text": "a"},
            {"start": 9.0, "end": 12.0, "text": "b"},
            {"start": 11.0, "end": 13.0, "text": "c"},
        ]
        out = asr.clamp_cues(cues, 10.0)
        self.assertEqual([c["text"] for c in out], ["a", "b"])
        self.assertEqual(out[1]["end"], 10.0)

    def test_zero_duration_is_a_noop(self):
        cues = [{"start": 1.0, "end": 2.0, "text": "a"}]
        self.assertEqual(asr.clamp_cues(cues, 0.0), cues)
