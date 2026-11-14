import shutil
import tempfile
import unittest
from pathlib import Path

import env  # noqa: F401  设置 DATA_DIR，必须最先导入

from app.scanner import scan_media
from app.subtitles import Cue, atomic_write, parse_srt, render_srt
from app.translation import OpenAITranslator, TranslationError


class PipelineTests(unittest.TestCase):
    def setUp(self):
        env.reset()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nested_index_only_videos(self):
        deep = self.tmp / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "movie.mkv").write_bytes(b"x")
        (deep / "note.txt").write_text("skip")
        (self.tmp / "top.mp4").write_bytes(b"x")
        result = scan_media(str(self.tmp))
        self.assertEqual(result["discovered"], 2)

    def test_srt_timecodes_and_atomic_output(self):
        text = "1\n00:00:01,000 --> 00:00:02,500\nこんにちは\n\n2\n00:01:02,000 --> 00:01:03,000\nさようなら\n"
        cues = parse_srt(text)
        self.assertEqual([c.text for c in cues], ["こんにちは", "さようなら"])
        out = self.tmp / "o.srt"
        atomic_write(out, render_srt(cues))
        self.assertEqual(parse_srt(out.read_text(encoding="utf-8"))[1].start, "00:01:02,000")
        with self.assertRaises(FileExistsError):
            atomic_write(out, "x")

    def test_translation_requires_all_ids_in_order(self):
        parser = OpenAITranslator("http://x", "", "m")._parse_lines
        good = '{"lines":[{"id":1,"text":"甲"},{"id":2,"text":"乙"}]}'
        self.assertEqual(parser(good, 2), ["甲", "乙"])
        for bad in ('{"lines":[{"id":2,"text":"乙"},{"id":1,"text":"甲"}]}',
                    '{"lines":[{"id":1,"text":"甲"}]}',
                    '{"lines":[{"id":1,"text":"  "},{"id":2,"text":"乙"}]}',
                    "not json"):
            with self.assertRaises(TranslationError):
                parser(bad, 2)

    def test_cue_render_roundtrip(self):
        cues = [Cue(1, "00:00:00,000", "00:00:01,000", "a"), Cue(2, "00:00:02,000", "00:00:03,000", "b")]
        self.assertEqual([c.text for c in parse_srt(render_srt(cues))], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
