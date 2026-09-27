from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation.meta import Meta


def response(status=200, payload=None):
    value = Mock(status_code=status, ok=status < 400)
    value.json.return_value = payload or {}
    return value


class MetaTests(unittest.TestCase):
    def make_client(self):
        with patch.dict("os.environ", {"META_PAGE_ID": "page", "META_PAGE_ACCESS_TOKEN": "token", "META_INSTAGRAM_ACCOUNT_ID": "ig-account"}):
            session = Mock()
            session.headers = {}
            return Meta(session=session)

    def test_long_video_is_unpublished(self):
        client = self.make_client()
        client.session.post.return_value = response(payload={"id": "video-id"})
        state = {}
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            result = client.upload_unpublished_video(video, {"title": "Title", "description": "Text"}, state, Mock())
        self.assertEqual(result, "video-id")
        self.assertEqual(client.session.post.call_args.kwargs["data"]["published"], "false")

    def test_long_video_includes_custom_thumbnail(self):
        client = self.make_client()
        client.session.post.return_value = response(payload={"id": "video-id"})
        state = {}
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"; video.write_bytes(b"video")
            cover = Path(tmp) / "thumbnail.jpg"; cover.write_bytes(b"jpeg")
            client.upload_unpublished_video(
                video, {"title": "Title", "description": "Text"}, state, Mock(), thumbnail=cover
            )
        files = client.session.post.call_args.kwargs["files"]
        self.assertEqual(set(files), {"source", "thumb"})
        self.assertEqual(files["thumb"][0], "thumbnail.jpg")

    def test_short_finishes_as_reel_draft(self):
        client = self.make_client()
        client.session.post.side_effect = [
            response(payload={"video_id": "reel-id", "upload_url": "https://rupload.facebook.com/video"}),
            response(payload={"success": True}),
            response(payload={"success": True}),
        ]
        state = {}
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            result = client.upload_reel_draft(video, {"title": "Title", "description": "Text"}, state, Mock())
        self.assertEqual(result, "reel-id")
        self.assertTrue(state["finished"])
        self.assertEqual(client.session.post.call_args.kwargs["data"]["video_state"], "DRAFT")

    def test_instagram_reel_is_uploaded_and_published(self):
        client = self.make_client()
        client.session.post.side_effect = [
            response(payload={"id": "container-id", "uri": "https://rupload.facebook.com/ig-api-upload/container"}),
            response(payload={"success": True}),
            response(payload={"id": "instagram-media-id"}),
        ]
        client.session.get.side_effect = [
            response(payload={"status_code": "FINISHED"}),
            response(payload={"permalink": "https://www.instagram.com/reel/example/"}),
        ]
        state = {}
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "short.mp4"; video.write_bytes(b"video")
            result = client.publish_instagram_reel(video, {"description": "Caption #Topic"}, state, Mock())
        self.assertEqual(result, "instagram-media-id")
        self.assertEqual(state["permalink"], "https://www.instagram.com/reel/example/")
        initiation = client.session.post.call_args_list[0]
        self.assertEqual(initiation.kwargs["data"]["media_type"], "REELS")
        self.assertEqual(initiation.kwargs["data"]["upload_type"], "resumable")
        self.assertEqual(initiation.kwargs["data"]["thumb_offset"], "1000")
        publish = client.session.post.call_args_list[2]
        self.assertEqual(publish.kwargs["data"], {"creation_id": "container-id"})

    def test_unknown_long_upload_is_not_retried(self):
        client = self.make_client()
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            metadata = {"title": "Title", "description": "Text"}
            state = {"started": True, "signature": client._signature(video, metadata)}
            with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                client.upload_unpublished_video(video, metadata, state, Mock())
        client.session.post.assert_not_called()
