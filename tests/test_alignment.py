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

    def test_weak_short_scene_is_interpolated_when_overall_match_is_good(self):
        scenes = [{"narration": "one two three four five six seven eight"},
                  {"narration": "Russell Wilson"}]
        recognized = [{"word": word, "start": index * .4, "end": index * .4 + .3}
                      for index, word in enumerate("one two three four five six seven eight mumble".split())]
        result = align_words(scenes, recognized, 4.2)
        self.assertEqual(result["scene_matched_fractions"], [1, 0])
        self.assertEqual(result["matched_fraction"], .8)
        self.assertGreaterEqual(result["boundaries"][1], result["words"][7]["end"])

    def test_verified_transcript_is_reused_and_scaled_after_resampling(self):
        import hashlib
        import json
        board = {"scenes": [{"narration": "Hello there"}, {"narration": "Good morning"}]}
        words = [{"word": word, "start": start, "end": start + .2} for word, start in
                 [("Hello", .1), ("there", .4), ("Good", 1.1), ("morning", 1.5)]]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root/'source.wav'; decoded = root/'decoded.wav'
            source.write_bytes(b'original audio'); decoded.write_bytes(b'resampled audio')
            (root/'transcript.json').write_text(json.dumps({
                "audio_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "model": "small", "words": words}))
            with patch('narration_alignment.transcribe') as transcribe, patch.object(p, 'duration', return_value=4):
                result = p.continuous_alignment(board, decoded, root, transcript_audio=source, timestamp_scale=2)
            transcribe.assert_not_called()
            self.assertAlmostEqual(result['boundaries'][1], 2.2)
            self.assertEqual(result['words'][-1]['end'], 3.4)

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
