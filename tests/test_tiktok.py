from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation.tiktok import TikTok


def response(status=200, payload=None):
    value = Mock(status_code=status, ok=status < 400)
    value.json.return_value = payload or {}
    return value


class TikTokTests(unittest.TestCase):
    def make_client(self):
        client = TikTok.__new__(TikTok)
        client.session = Mock()
        client.access_token = "access"
        return client

    def test_refresh_saves_rotated_refresh_token(self):
        session = Mock()
        session.post.return_value = response(payload={
            "access_token": "access", "refresh_token": "new-refresh", "open_id": "owner"
        })
        state = {}
        with patch.dict("os.environ", {
            "TIKTOK_CLIENT_KEY": "key", "TIKTOK_CLIENT_SECRET": "secret",
            "TIKTOK_REFRESH_TOKEN": "initial",
        }):
            TikTok(state, Mock(), session=session)
        self.assertEqual(state, {"refresh_token": "new-refresh", "open_id": "owner"})

    def test_uploads_one_file_as_inbox_draft(self):
        client = self.make_client()
        client.session.post.return_value = response(payload={
            "data": {"publish_id": "draft-1", "upload_url": "https://open-upload.tiktokapis.com/video/id"}
        })
        client.session.put.return_value = response()
        state = {}
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            publish_id = client.upload_draft(video, state, Mock())
        self.assertEqual(publish_id, "draft-1")
        self.assertTrue(state["uploaded"])
        self.assertNotIn("upload_url", state)
        self.assertEqual(client.session.put.call_args.kwargs["headers"]["Content-Range"], "bytes 0-4/5")

    def test_unknown_upload_does_not_resend_bytes(self):
        client = self.make_client()
        client.status = Mock(return_value={"status": "PROCESSING_UPLOAD", "uploaded_bytes": 0})
        state = {
            "signature": "unused", "publish_id": "draft", "upload_url": "https://open-upload.tiktokapis.com/video/id",
            "upload_started": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"video")
            state["signature"] = __import__("hashlib").sha256(b"video").hexdigest()
            with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                client.upload_draft(video, state, Mock())
        client.session.put.assert_not_called()
