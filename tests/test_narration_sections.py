from pathlib import Path
import io
import wave
import tempfile
import unittest
from unittest.mock import Mock, patch

import pipeline
from narration_sections import join_wavs, split_sections

def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(22050)
        wav.writeframes(b"\\x00\\x00" * 2205)
    return buffer.getvalue()


class NarrationSectionTests(unittest.TestCase):
    def scenes(self, count, words=75):
        return [{"narration": " ".join([f"scene{index}"] + ["detail"] * (words - 2) + ["ends."])}
                for index in range(1, count + 1)]

    def test_short_narration_stays_in_one_call(self):
        scenes = self.scenes(2, 50)
        self.assertEqual(split_sections(scenes), [" ".join(scene["narration"] for scene in scenes)])

    def test_short_form_policy_splits_long_script_at_sentence_boundary(self):
        scenes = [
            {"narration": " ".join([f"Sentence {index}"] + ["detail"] * 13 + ["ends."])}
            for index in range(1, 7)
        ]
        sections = split_sections(scenes, threshold=70, target=45, minimum=25, maximum=55)
        self.assertGreaterEqual(len(sections), 2)
        self.assertEqual(" ".join(sections), " ".join(scene["narration"] for scene in scenes))
        self.assertTrue(all(section.endswith("ends.") for section in sections))
        self.assertTrue(all(25 <= len(section.split()) <= 55 for section in sections))

    def test_one_minute_narration_splits_at_sentence_boundaries(self):
        scenes = self.scenes(11, 16)
        sections = split_sections(scenes)
        self.assertEqual(len(sections), 3)
        self.assertEqual(" ".join(sections), " ".join(scene["narration"] for scene in scenes))
        self.assertTrue(all(section.endswith("ends.") for section in sections))

    def test_does_not_split_after_initials_or_inside_a_sentence(self):
        scenes = [{"narration": "Warren G. Harding served as president. " * 8},
                  {"narration": "The U.S. changed over time. " * 8}]
        sections = split_sections(scenes)
        self.assertTrue(all(not section.endswith("Warren G.") for section in sections))
        self.assertTrue(all(not section.endswith("The U.S.") for section in sections))
        self.assertEqual(" ".join(sections), " ".join(scene["narration"].strip() for scene in scenes))

    def test_long_narration_splits_only_between_complete_scenes(self):
        scenes = self.scenes(12, 75)
        sections = split_sections(scenes)
        self.assertGreater(len(sections), 1)
        self.assertEqual(" ".join(sections), " ".join(scene["narration"] for scene in scenes))
        self.assertTrue(all(section.endswith("ends.") for section in sections))
        self.assertTrue(all(52 <= len(section.split()) <= 85 for section in sections))

    def test_section_generation_is_resumable(self):
        runpod = Mock()
        runpod.fingerprint_data.side_effect = lambda text: {"text": text}
        runpod.synthesize.return_value = wav_bytes()
        with tempfile.TemporaryDirectory() as temp:
            assets = Path(temp) / "assets"; assets.mkdir()
            def fake_join(parts, output, *_args, **_kwargs):
                output.write_bytes(parts[0].read_bytes())
            with patch.object(pipeline, "check_runpod_narration"), \
                 patch.object(pipeline, "check_joined_narration"), \
                 patch("narration_sections.join_wavs", side_effect=fake_join) as join:
                pipeline.generate_sectioned_narration(
                    assets, runpod, ["First written.", "Second written."],
                    ["First spoken.", "Second spoken."])
                pipeline.generate_sectioned_narration(
                    assets, runpod, ["First written.", "Second written."],
                    ["First spoken.", "Second spoken."])
            self.assertEqual(runpod.synthesize.call_count, 2)
            self.assertEqual(join.call_count, 1)
            self.assertTrue((assets / "narration.wav").is_file())

    def test_join_adds_a_natural_transition_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); first = root / "first.wav"; second = root / "second.wav"
            output = root / "joined.wav"
            pipeline.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=330:duration=1", str(first)])
            pipeline.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(second)])
            join_wavs([first, second], output, pipeline.run_ffmpeg, pause_seconds=.26)
            self.assertAlmostEqual(pipeline.duration(output), 2.26, delta=.12)


if __name__ == "__main__":
    unittest.main()
