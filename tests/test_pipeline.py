import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import pipeline as p


def board():
    return {"title": "Test", "style_guide": "Watercolor", "aspect_ratio": "9:16",
            "scenes": [{"id": i, "narration": text, "prompt": "Ocean waves",
                        "camera_style": "Wide", "mood": "Calm", "estimated_duration": 1,
                        "motion": "zoom" if i == 1 else "still", "transition": "fade" if i == 1 else "cut"}
                       for i, text in enumerate(["One short scene.", "Another scene follows."], 1)]}


class PipelineTests(unittest.TestCase):
    def test_speech_requires_voice_and_uses_correct_custom_field(self):
        with self.assertRaisesRegex(ValueError, "requires a voice"):
            p.asset_payloads(board(), board()["scenes"][0], " ")
        speech, _ = p.asset_payloads(board(), board()["scenes"][0], "speech:custom:123")
        self.assertEqual(speech["voice"], "speech:custom:123")
        self.assertNotIn("voice_uri", speech)

    def test_validation_error_redacts_key(self):
        response = Mock(ok=False, status_code=400)
        response.json.return_value = {"code": 20012, "message": "voice is required; sk-test-secret"}
        with patch.dict(p.os.environ, {"SILICONFLOW_API_KEY": "sk-test-secret"}), patch.object(
            p.requests, "post", return_value=response
        ):
            with self.assertRaisesRegex(RuntimeError, "voice is required") as error:
                p.api_post("https://api.siliconflow.com/v1", "SILICONFLOW_API_KEY", "/audio/speech", {})
            self.assertNotIn("sk-test-secret", str(error.exception))

    def test_429_distinguishes_quota_and_rate_limits(self):
        for error, expected in [
            ({"code": "insufficient_quota"}, "Check API billing"),
            ({"code": "credit_balance_exhausted", "type": "insufficient_quota"}, "Add API credits"),
            ({"code": "rate_limit_exceeded"}, "Wait at least 20 seconds"),
            ({"code": None, "type": "insufficient_quota"}, "Check API billing"),
            ({}, "unknown_429"),
        ]:
            with self.subTest(error=error):
                response = Mock(ok=False, status_code=429, headers={"Retry-After": "20"})
                response.json.return_value = {"error": error}
                with patch.dict(p.os.environ, {"OPENAI_API_KEY": "test"}), patch.object(
                    p.requests, "post", return_value=response
                ) as post:
                    with self.assertRaisesRegex(RuntimeError, expected):
                        p.api_post("https://api.openai.com/v1", "OPENAI_API_KEY", "/responses", {}, retries=0)
                    self.assertEqual(post.call_count, 1)

    def test_timeout_is_clear_and_not_retried(self):
        with patch.dict(p.os.environ, {"OPENAI_API_KEY": "test"}), patch.object(
            p.requests, "post", side_effect=p.requests.ReadTimeout
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "OpenAI writing/planning timed out"):
                p.api_post("https://api.openai.com/v1", "OPENAI_API_KEY", "/responses", {})
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["timeout"], (15, 120))

    def test_rate_limit_retries_then_succeeds(self):
        success = Mock()
        with patch.object(p, "_api_post_once", side_effect=[p.TemporaryRateLimit("limited", "45"), success]) as request, patch.object(
            p.time, "sleep"
        ) as sleep, patch.object(p.random, "uniform", return_value=0):
            self.assertIs(p.api_post("base", "key", "endpoint", {}), success)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(sum(c.args[0] for c in sleep.call_args_list), 45)

    def test_temporary_server_error_retries_then_succeeds(self):
        failure = Mock(ok=False, status_code=500, headers={'x-request-id': 'req-test'})
        success = Mock(ok=True)
        with patch.dict(p.os.environ, {'OPENAI_API_KEY': 'test'}), patch.object(
            p.requests, 'post', side_effect=[failure, success]
        ) as post, patch.object(p.time, 'sleep') as sleep, patch.object(p.random, 'uniform', return_value=0):
            self.assertIs(p.api_post('https://api.openai.com/v1', 'OPENAI_API_KEY', '/images/generations', {}), success)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), 10)

    def test_rate_limit_retries_are_bounded(self):
        with patch.object(p, "_api_post_once", side_effect=p.TemporaryRateLimit("limited", "")) as request, patch.object(p.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Retry limit reached"):
                p.api_post("base", "key", "endpoint", {})
            self.assertEqual(request.call_count, 4)

    def test_long_cooldown_does_not_retry_early(self):
        with patch.object(p, "_api_post_once", side_effect=p.TemporaryRateLimit("limited", "3600")) as request, patch.object(p.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "long wait"):
                p.api_post("base", "key", "endpoint", {})
            request.assert_called_once()
            sleep.assert_not_called()

    def test_cancel_exits_without_traceback(self):
        with patch.object(p.sys, "argv", ["pipeline.py", "render", "--out", "unused"]), patch.object(
            p.Path, "read_text", side_effect=KeyboardInterrupt
        ), patch("sys.stderr") as stderr:
            with self.assertRaises(SystemExit) as result:
                p.main()
            self.assertEqual(result.exception.code, 130)
            self.assertIn("Cancelled", "".join(c.args[0] for c in stderr.write.call_args_list))

    def test_source_narration_is_attached_locally(self):
        response = Mock()
        response.json.return_value = {"status": "completed", "output": [
            {"content": [{"type": "output_text", "text": json.dumps(board())}]}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, "api_post", return_value=response):
            output = Path(tmp) / "storyboard.json"
            result = p.plan("A completely different script.", output, 2, "Watercolor", "9:16")
            self.assertEqual(" ".join(s["narration"] for s in result["scenes"]), "A completely different script.")
            self.assertTrue(all(s["narration"].strip() for s in result["scenes"]))
            self.assertTrue(output.exists())

    def test_bad_scene_ids(self):
        value = board()
        value["scenes"][1]["id"] = 1
        with self.assertRaises(ValueError):
            p.validate_board(value)

    def test_caption_scene_offsets(self):
        result = p.captions([{"narration": "One two three four five six seven",
                              "start": 2, "audio_duration": 7}])
        self.assertIn("00:00:02,000 --> 00:00:08,000", result)
        self.assertIn("00:00:08,000 --> 00:00:09,000", result)
        self.assertEqual(p.timestamp(59.9999), "00:01:00,000")

    def test_short_captions_are_smaller_and_raised_above_platform_controls(self):
        short = p.caption_style_metrics(1080, 1920)
        long = p.caption_style_metrics(1920, 1080)
        self.assertEqual(short['font_size'], 100)
        self.assertEqual(short['margin_v'], 346)
        self.assertEqual(short['margin_h'], 86)
        self.assertEqual(long['font_size'], 67)
        self.assertEqual(long['margin_v'], 59)
        ass = p.styled_captions('1\n00:00:00,000 --> 00:00:01,000\nA SHORT CAPTION\n', 1080, 1920)
        self.assertIn('Arial,100,', ass)
        self.assertIn(',2,86,86,346,1', ass)

    def test_resume_and_invalidate_only_changed_asset(self):
        audio, image = Mock(), Mock()
        audio.content = b"test audio"
        audio.headers = {"Content-Type": "audio/mpeg"}
        image.json.return_value = {"data": [{"b64_json": "aW1hZ2U="}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                p, "api_post", side_effect=[audio, image, audio, image, image]) as api, \
                patch.object(p, "check_image_text"):
            value = board()
            p.generate_assets(value, Path(tmp), "voice")
            self.assertEqual(api.call_count, 4)
            p.generate_assets(value, Path(tmp), "voice")
            self.assertEqual(api.call_count, 4)
            value["scenes"][0]["prompt"] = "Changed visual"
            p.generate_assets(value, Path(tmp), "voice")
            self.assertEqual(api.call_count, 5)

    def test_gpt_image_2_default_reuses_paid_legacy_image(self):
        audio, image = Mock(), Mock()
        audio.content = b"test audio"; audio.headers = {"Content-Type": "audio/mpeg"}
        image.json.return_value = {"data": [{"b64_json": "aW1hZ2U="}]}
        value = board(); value['scenes'] = value['scenes'][:1]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                p, "api_post", side_effect=[audio, image]) as api, patch.object(p, "check_image_text"):
            root = Path(tmp)
            with patch.dict(p.os.environ, {"OPENAI_IMAGE_MODEL": "gpt-image-1.5"}):
                p.generate_assets(value, root, "voice")
            with patch.dict(p.os.environ, {}, clear=False):
                p.os.environ.pop("OPENAI_IMAGE_MODEL", None)
                p.generate_assets(value, root, "voice")
            self.assertEqual(api.call_count, 2)

    def test_real_render(self):
        # Real FFmpeg smoke test: two scenes, zoom/fade/cut, music, burned and soft captions.
        with tempfile.TemporaryDirectory(prefix="video test '") as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            for i in (1, 2):
                p.run_ffmpeg(["-f", "lavfi", "-i", f"color=c={'blue' if i == 1 else 'green'}:s=180x320",
                              "-frames:v", "1", str(assets / f"scene-{i:03}.png")])
                p.run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=0.73",
                              str(assets / f"scene-{i:03}.{'mp3' if i == 1 else 'wav'}")])
            music = assets / "scene-001.mp3"
            p.run_ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=180x320:r=15:d=0.4",
                          str(assets / "scene-001.animation.mp4")])
            (root / "animation-manifest.json").write_text(json.dumps({
                "enabled": True, "animated": [1], "fallback": []}))
            for mode in ("burn", "soft", "none"):
                with patch.dict(p.os.environ, {"ENABLE_ANIMATION": "1"}):
                    p.render(board(), root, height=320, fps=15, subtitle_mode=mode, music=music)
                self.assertTrue((root / "video.mp4").is_file())
                timeline = json.loads((root / "timeline.json").read_text())
                self.assertAlmostEqual(p.duration(root / "video.mp4"), timeline["duration"], delta=0.12)
                self.assertEqual(timeline["scenes"][1]["start"], timeline["scenes"][0]["end"])
                self.assertTrue(timeline["scenes"][0]["animated"])
                self.assertFalse(timeline["scenes"][1]["animated"])
            with patch.dict(p.os.environ, {"ENABLE_ANIMATION": "0"}):
                p.render(board(), root, height=320, fps=15, subtitle_mode="none", music=music)
            timeline = json.loads((root / "timeline.json").read_text())
            self.assertFalse(any(scene["animated"] for scene in timeline["scenes"]))


if __name__ == "__main__":
    unittest.main()

class PlannerCorrectionTests(unittest.TestCase):
    def test_wrong_aspect_is_retried_once_with_exact_schema(self):
        wrong = board(); wrong['aspect_ratio'] = '16:9'
        right = board(); right['aspect_ratio'] = '9:16'
        def response(value):
            result = Mock(); result.json.return_value = {'status': 'completed', 'output': [
                {'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}
            return result
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                p, 'api_post', side_effect=[response(wrong), response(right)]) as post:
            result = p.plan('One short scene. Another scene follows.', Path(tmp)/'storyboard.json', 2, 'Ink', '9:16')
            self.assertEqual(result['aspect_ratio'], '9:16')
            self.assertEqual(post.call_count, 2)
            retry_input = json.loads(post.call_args.args[3]['input'])
            self.assertIn('correction', retry_input)
            schema = post.call_args.args[3]['text']['format']['schema']
            self.assertEqual(schema['properties']['scenes']['minItems'], 2)
            self.assertEqual(schema['properties']['scenes']['maxItems'], 2)
