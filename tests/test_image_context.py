import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

import pipeline as p
from test_pipeline import board


class ImageContextTests(unittest.TestCase):
    def test_entire_script_and_target_scene_in_prompt(self):
        value = board()
        value["scenes"][0]["show_character"] = False
        prompt = p.image_prompt(value, value["scenes"][0])
        self.assertIn("One short scene. Another scene follows.", prompt)
        self.assertIn('"target_scene": 1', prompt)
        self.assertIn("Do not show the narrator", prompt)
        self.assertIn("depict only the target scene", prompt)

    def test_long_form_reserves_conclusion_composition_for_final_scene(self):
        value = board()
        value['aspect_ratio'] = '16:9'
        value['scenes'].append({**value['scenes'][-1], 'id': 3, 'narration': 'The final answer.'})
        body_prompt = p.image_prompt(value, value['scenes'][1])
        final_prompt = p.image_prompt(value, value['scenes'][2])
        self.assertIn('BODY SCENE: This is not the conclusion', body_prompt)
        self.assertIn('Do not use a wrap-up pose', body_prompt)
        self.assertNotIn('FINAL CONCLUSION SCENE:', body_prompt)
        self.assertIn('FINAL CONCLUSION SCENE:', final_prompt)
        self.assertNotIn('BODY SCENE:', final_prompt)

    def test_body_scene_text_fallback_does_not_become_a_conclusion(self):
        value = board()
        value['aspect_ratio'] = '16:9'
        prompt = p.text_safe_fallback_prompt(p.image_prompt(value, value['scenes'][0]))
        self.assertIn('do not turn it into a reflective or concluding tableau', prompt)

    def test_character_reference_sent_as_multipart(self):
        response = Mock(ok=True)
        with patch.dict(p.os.environ, {"OPENAI_API_KEY": "test"}), patch.object(p.requests, "post", return_value=response) as post:
            p.api_post("https://api.openai.com/v1", "OPENAI_API_KEY", "/images/edits",
                       {"prompt": "A drawing", "model": "gpt-image-1.5"}, image_reference=b"reference")
            kwargs = post.call_args.kwargs
            self.assertNotIn("json", kwargs)
            self.assertEqual(kwargs["files"]["image"][1], b"reference")
            self.assertEqual(kwargs["data"]["prompt"], "A drawing")

    def test_reference_changes_only_regenerate_character_images(self):
        value = board()
        value["scenes"][0]["show_character"] = True
        value["scenes"][1]["show_character"] = False
        audio = Mock(content=b"audio", headers={"Content-Type": "audio/mpeg"})
        image = Mock()
        image.json.return_value = {"data": [{"b64_json": "aW1hZ2U="}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref = root / "base.png"
            ref.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
            with patch.object(p, "api_post", side_effect=[audio, image, audio, image, image]) as api, \
                 patch.object(p, "check_image_text"):
                p.generate_assets(value, root, "voice", image_reference=ref)
                self.assertEqual(api.call_args_list[1].args[2], "/images/edits")
                self.assertEqual(api.call_args_list[3].args[2], "/images/generations")
                ref.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
                p.generate_assets(value, root, "voice", image_reference=ref)
                self.assertEqual(api.call_count, 5)

class ImageSafetyFallbackTests(unittest.TestCase):
    def test_safety_rejection_uses_neutral_generation_fallback(self):
        from test_pipeline import board
        from test_runpod_tts import wav_bytes
        value = board(); value['scenes'] = value['scenes'][:1]
        value['scenes'][0]['prompt'] = 'A branded violent game scene'
        runpod = Mock(model_version='2.5')
        runpod.fingerprint_data.side_effect = lambda text: {'text': text}
        runpod.synthesize.return_value = wav_bytes()
        image = Mock(); image.json.return_value = {'data': [{'b64_json': 'aW1hZ2U='}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                p, 'api_post', side_effect=[RuntimeError('request rejected by the safety system'), image]) as api, \
                patch.object(p, 'check_runpod_narration'), patch.object(p, 'check_image_text'):
            p.generate_assets(value, Path(tmp)/'assets', '', runpod=runpod, narration_mode='continuous')
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args[2], '/images/generations')
            self.assertNotIn('branded violent game', api.call_args.args[3]['prompt'])
            self.assertTrue((Path(tmp)/'assets/scene-001.prompt-safety-fallback.txt').exists())
            self.assertTrue((Path(tmp)/'assets/scene-001.png').exists())
