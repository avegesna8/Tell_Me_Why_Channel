from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import pipeline as p
from narration_alignment import align_words


class AlignmentTests(unittest.TestCase):
    def test_scene_switch_uses_speech_not_word_count(self):
        scenes = [{"narration": "Hello there."}, {"narration": "Good morning."}]
        words = [{"word": w, "start": st, "end": en} for w, st, en in
                 [("Hello", 0.2, 0.5), ("there", 0.5, 0.8), ("Good", 3.0, 3.5), ("morning", 3.5, 4.0)]]
        result = align_words(scenes, words, 4.5)
        self.assertEqual(result["boundaries"], [0.0, 3.0, 4.5])
        self.assertEqual(result["words"][-1]["word"], "morning.")

    def test_unrelated_speech_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not closely match"):
            align_words([{"narration": "Deep beneath the ocean"}],
                        [{"word": "unrelated", "start": 0, "end": 1}], 1)

    def test_one_generation_for_whole_script(self):
        from test_pipeline import board
        from test_runpod_tts import wav_bytes
        value = board()
        runpod = Mock()
        runpod.model_version = "2.5"
        runpod.fingerprint_data.side_effect = lambda text: {"text": text}
        runpod.synthesize.return_value = wav_bytes()
        image = Mock()
        image.json.return_value = {"data": [{"b64_json": "aW1hZ2U="}]}
        with tempfile.TemporaryDirectory() as temp, patch.object(p, "api_post", return_value=image) as api, \
             patch.object(p, "check_runpod_narration"), patch.object(p, "check_image_text"):
            p.generate_assets(value, Path(temp), "", runpod=runpod, narration_mode="continuous")
            runpod.synthesize.assert_called_once()
            self.assertEqual(runpod.synthesize.call_args.args[0], "One short scene. Another scene follows.")
            self.assertEqual(api.call_count, 2)
            self.assertTrue((Path(temp)/"narration.wav").exists())
            p.generate_assets(value, Path(temp), "", runpod=runpod, narration_mode="continuous")
            self.assertEqual(runpod.synthesize.call_count, 1)
            self.assertEqual(api.call_count, 2)

    def test_continuous_render_audio_survives_scene_boundary(self):
        from test_pipeline import board
        import array
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = root / "assets"
            assets.mkdir()
            p.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.46", str(assets/"narration.wav")])
            for i in (1, 2):
                p.run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(assets/f"scene-{i:03}.png")])
            alignment = {"boundaries": [0, 0.73, 1.46], "words": [
                {"word": "Hello", "start": 0.1, "end": 0.5}, {"word": "World", "start": 0.8, "end": 1.4}]}
            with patch.object(p, "continuous_alignment", return_value=alignment):
                for mode in ("burn", "soft", "none"):
                    p.render(board(), root, height=320, fps=15, subtitle_mode=mode,
                             narration_mode="continuous", music=assets/"narration.wav" if mode == "soft" else None)
                    pcm = subprocess.run([p.binary("ffmpeg"), "-v", "error", "-i", str(root/"video.mp4"),
                                          "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
                    samples = array.array("h", pcm)
                    # Continuous tone must not acquire a silent seam at the image change.
                    for start in range(33000, 37000, 240):
                        chunk = samples[start:start+240]
                        self.assertGreater(sum(x*x for x in chunk)/len(chunk), 100000)
                    self.assertAlmostEqual(len(samples)/48000, 1.46, delta=0.04)
