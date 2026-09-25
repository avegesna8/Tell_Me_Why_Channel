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
            with patch.object(p, "api_post", side_effect=[audio, image, audio, image, image]) as api:
                p.generate_assets(value, root, "voice", image_reference=ref)
                self.assertEqual(api.call_args_list[1].args[2], "/images/edits")
                self.assertEqual(api.call_args_list[3].args[2], "/images/generations")
                ref.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
                p.generate_assets(value, root, "voice", image_reference=ref)
                self.assertEqual(api.call_count, 5)
