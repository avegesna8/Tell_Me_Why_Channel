import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pipeline as p


class IdeaTests(unittest.TestCase):
    def test_automatic_pacing(self):
        self.assertEqual(p.automatic_scene_count("word " * 405), 27)
        self.assertEqual(p.automatic_scene_count("word " * 405, 3), 54)
        self.assertEqual(p.automatic_scene_count("word " * 10000), 100)
        with self.assertRaises(ValueError):
            p.automatic_scene_count("hello", float("nan"))

    def test_saved_script_resumes_and_changed_brief_is_rejected(self):
        response = Mock()
        response.json.return_value = {"status": "completed", "output": [{"content": [
            {"type": "output_text", "text": json.dumps({"title": "IMAX", "narration": "A larger picture."})}]}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, "api_post", return_value=response) as post:
            root = Path(tmp)
            script = p.script_from_idea("What is IMAX?", root)
            script.write_text("My edited narration.")
            self.assertEqual(p.script_from_idea("What is IMAX?", root).read_text(), "My edited narration.")
            with self.assertRaisesRegex(ValueError, "different idea/length"):
                p.script_from_idea("Other topic", root)
            self.assertEqual(post.call_count, 1)
            script.unlink()
            self.assertEqual(p.script_from_idea("What is IMAX?", root).read_text().strip(), "A larger picture.")
            self.assertEqual(post.call_count, 1)

    def test_bad_input_and_incomplete_output_are_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, "api_post") as post:
            root = Path(tmp)
            for idea, minutes in [(" ", 3), ("Topic", float("nan")), ("Topic", -1)]:
                with self.assertRaises(ValueError):
                    p.script_from_idea(idea, root, minutes)
            post.assert_not_called()
            post.return_value.json.return_value = {"status": "incomplete"}
            with self.assertRaises(RuntimeError):
                p.script_from_idea("Topic", root)
            self.assertFalse((root / "idea.json").exists())
            self.assertFalse((root / "script.txt").exists())

    def test_existing_project_cannot_be_repurposed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, "api_post") as post:
            root = Path(tmp)
            (root / "storyboard.json").write_text("{}")
            with self.assertRaises(ValueError):
                p.script_from_idea("Topic", root)
            post.assert_not_called()
