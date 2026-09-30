import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import animation


def board(aspect="16:9", count=8):
    return {
        "aspect_ratio": aspect,
        "scenes": [
            {"id": index, "prompt": f"Scene {index}"}
            for index in range(1, count + 1)
        ],
    }


def response(payload=None, content=b"", ok=True, status=200):
    value = Mock(ok=ok, status_code=status, content=content)
    value.json.return_value = payload or {}
    return value


class AnimationTests(unittest.TestCase):
    def test_disabled_is_a_clean_noop(self):
        with patch.dict("os.environ", {"ENABLE_ANIMATION": "0"}, clear=True):
            self.assertEqual(
                animation.generate_animations(board(), Path("/unused"), "long"),
                {"enabled": False, "selected": [], "animated": [], "fallback": []},
            )

    def test_scene_selection_is_bounded_and_spread_out(self):
        self.assertEqual(animation.selected_scene_ids(board(count=8), 1), [5])
        self.assertEqual(animation.selected_scene_ids(board(count=12), 3), [4, 7, 9])
        self.assertEqual(len(animation.selected_scene_ids(board(count=2), 3)), 2)

    def test_successful_clip_is_downloaded_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "scene-005.png").write_bytes(b"png")
            session = Mock()
            session.post.return_value = response({"id": "task-1"})
            session.get.side_effect = [
                response({"status": "SUCCEEDED", "output": ["https://cdn.example/video.mp4"]}),
                response(content=b"video"),
            ]
            env = {
                "ENABLE_ANIMATION": "1",
                "RUNWAYML_API_SECRET": "secret",
                "ANIMATION_LONG_SCENES": "1",
            }
            with patch.dict("os.environ", env, clear=True), patch.object(animation.time, "sleep"):
                report = animation.generate_animations(board(), root, "long", session)
                again = animation.generate_animations(board(), root, "long", session)
            self.assertEqual(report["animated"], [5])
            self.assertEqual(again["animated"], [5])
            self.assertEqual((assets / "scene-005.animation.mp4").read_bytes(), b"video")
            self.assertEqual(session.post.call_count, 1)
            payload = session.post.call_args.kwargs["json"]
            self.assertEqual(payload["ratio"], "1280:720")
            self.assertEqual(payload["duration"], 5)

    def test_two_terminal_failures_fall_back_to_still(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "scene-001.png").write_bytes(b"png")
            session = Mock()
            session.post.side_effect = [response({"id": "one"}), response({"id": "two"})]
            session.get.side_effect = [
                response({"status": "FAILED", "failureCode": "bad"}),
                response({"status": "FAILED", "failureCode": "bad"}),
            ]
            env = {
                "ENABLE_ANIMATION": "1",
                "RUNWAYML_API_SECRET": "secret",
                "ANIMATION_SHORT_SCENES": "1",
            }
            with patch.dict("os.environ", env, clear=True), patch.object(animation.time, "sleep"):
                report = animation.generate_animations(board("9:16", 1), root, "short", session)
            self.assertEqual(report["animated"], [])
            self.assertEqual(report["fallback"][0]["reason"], "generation_failed")
            self.assertFalse((assets / "scene-001.animation.mp4").exists())
            self.assertEqual(session.post.call_count, 2)
            self.assertEqual(session.post.call_args.kwargs["json"]["ratio"], "720:1280")


    def test_insufficient_funds_stops_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"
            assets.mkdir()
            (assets / "scene-005.png").write_bytes(b"png")
            session = Mock()
            session.post.return_value = response(
                {"error": "insufficient_credits"}, ok=False, status=402
            )
            env = {
                "ENABLE_ANIMATION": "1",
                "RUNWAYML_API_SECRET": "secret",
                "ANIMATION_LONG_SCENES": "1",
            }
            with patch.dict("os.environ", env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "Runway has insufficient funds"):
                    animation.generate_animations(board(), root, "long", session)

    def test_enabled_requires_key_before_spending(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"ENABLE_ANIMATION": "1"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "RUNWAYML_API_SECRET"):
                animation.generate_animations(board(), Path(tmp), "long")


if __name__ == "__main__":
    unittest.main()
