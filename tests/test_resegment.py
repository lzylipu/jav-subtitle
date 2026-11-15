"""词级重切句：这是「音画不同步」的修复点，必须锁住。

真值场景：15 句日语对白散落在 370 秒里，每句之间隔十几秒。
旧版只开 vad_filter，whisper 会把整块连续语音当一个 segment，
一条字幕横跨 60～200 秒，和画面对不上。
开 word_timestamps 后按「词间静音 > gap」重切，起点偏差从 41 秒降到 0.26 秒。
"""
import unittest

from tests import env  # noqa: F401  必须在导入 app 之前设置 DATA_DIR

from app import asr


def w(start, end, text):
    return {"start": start, "end": end, "text": text, "prob": 0.9}


class ResegmentTests(unittest.TestCase):
    def test_silence_gap_starts_a_new_cue(self):
        words = [w(1.0, 1.4, "おはよう"), w(1.4, 1.9, "ございます"),
                 w(15.0, 15.5, "はい"), w(15.5, 16.0, "そうです")]
        cues = asr.resegment(words, gap=0.7)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "おはようございます")
        self.assertEqual(cues[1]["text"], "はいそうです")
        # 时间轴严格贴着发声区间，不外扩
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertAlmostEqual(cues[0]["end"], 1.9)
        self.assertAlmostEqual(cues[1]["start"], 15.0)

    def test_short_pause_does_not_split(self):
        # 0.3 秒的停顿是正常换气，不该断句
        words = [w(1.0, 1.5, "ちょっと"), w(1.8, 2.4, "待って")]
        self.assertEqual(len(asr.resegment(words, gap=0.7)), 1)

    def test_long_cue_is_cut_by_duration(self):
        # 连续说话不停顿，靠时长上限也要断开，否则一条字幕挂十几秒
        words = [w(i * 1.0, i * 1.0 + 0.9, "あ") for i in range(20)]
        cues = asr.resegment(words, gap=0.7, max_seconds=5.0, max_chars=200)
        self.assertGreater(len(cues), 1)
        for cue in cues:
            self.assertLessEqual(cue["end"] - cue["start"], 6.0)

    def test_long_cue_prefers_punctuation(self):
        # 超过字数上限时不能把句子从中间砍断，要在句号后面切
        words = [w(0.0, 0.3, "これは"), w(0.3, 0.6, "テストです。"),
                 w(0.6, 0.9, "つぎの"), w(0.9, 1.2, "ぶんです。"),
                 w(1.2, 1.5, "さらに"), w(1.5, 1.8, "つづく")]
        cues = asr.resegment(words, gap=0.7, max_seconds=999, max_chars=12)
        self.assertGreater(len(cues), 1)
        # 每一条（除最后一条）都应该以句号结尾，而不是从词中间硬切
        for cue in cues[:-1]:
            self.assertTrue(cue["text"].endswith("。"), cue["text"])

    def test_empty_input(self):
        self.assertEqual(asr.resegment([]), [])

    def test_no_blank_cues(self):
        # 只有空白的词不该产出空字幕条
        words = [w(0.0, 0.2, " "), w(5.0, 5.2, "  ")]
        self.assertEqual(asr.resegment(words), [])


class WordTimestampWiringTests(unittest.TestCase):
    """确保 transcribe_audio 真的把 word_timestamps 传下去、且关批推理。

    这两处任何一处回退，字幕时间轴就会重新变成「一条挂几分钟」。
    """

    def test_word_timestamps_requested_by_default(self):
        captured = {}

        class FakeWord:
            def __init__(self, start, end, word):
                self.start, self.end, self.word, self.probability = start, end, word, 0.9

        class FakeSegment:
            start, end, text = 0.0, 3.0, "ダミー"
            words = [FakeWord(0.0, 1.0, "ダミー"), FakeWord(30.0, 31.0, "ツー")]

        class FakeModel:
            def transcribe(self, path, **kwargs):
                captured.update(kwargs)
                info = type("I", (), {"duration": 300.0, "language": "ja"})()
                return iter([FakeSegment()]), info

        class FakeBatched:  # 真实环境里 BatchedInferencePipeline 永远存在，只是 batch_size<=1 不用它
            def transcribe(self, *a, **k):  # pragma: no cover
                raise AssertionError("batch_size=1 时不该走批推理")

        original = asr.get_model
        # 关键：这里必须返回非 None 的 batched，否则测不出「有 batched 但 batch_size=1」这条真实路径
        asr.get_model = lambda *a, **k: (FakeModel(), FakeBatched())
        try:
            segments, _lang, _dur = asr.transcribe_audio(
                __import__("pathlib").Path("/dev/null"), "small", 4, "ja",
                beam_size=5, batch_size=1)
        finally:
            asr.get_model = original

        self.assertTrue(captured.get("word_timestamps"), "word_timestamps 没传下去")
        self.assertFalse(captured.get("condition_on_previous_text", True),
                         "condition_on_previous_text 必须关，否则会串话")
        # 两个词相隔 29 秒，必须切成两条
        self.assertEqual(len(segments), 2)

    def test_word_timestamps_can_be_disabled(self):
        captured = {}

        class FakeSegment:
            start, end, text = 0.0, 120.0, "ながい"
            words = None

        class FakeModel:
            def transcribe(self, path, **kwargs):
                captured.update(kwargs)
                info = type("I", (), {"duration": 120.0, "language": "ja"})()
                return iter([FakeSegment()]), info

        original = asr.get_model
        asr.get_model = lambda *a, **k: (FakeModel(), None)
        try:
            segments, _l, _d = asr.transcribe_audio(
                __import__("pathlib").Path("/dev/null"), "small", 4, "ja",
                word_timestamps=False)
        finally:
            asr.get_model = original
        self.assertNotIn("word_timestamps", captured)
        self.assertEqual(segments[0]["text"], "ながい")


if __name__ == "__main__":
    unittest.main()
