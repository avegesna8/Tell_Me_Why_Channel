import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from runpod_tts import RunPodTTS
import pipeline


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 2205)
    return buffer.getvalue()


class RunPodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ref = self.root / "ref.wav"
        ref.write_bytes(wav_bytes())
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "test"}):
            self.client = RunPodTTS("https://api.runpod.ai/v2/endpoint-1/runsync", ref, poll_seconds=0)
        self.state = self.root / "job.json"
        self.completed = {"id": "job-1", "status": "COMPLETED", "output": {
            "ok": True, "format": "wav", "audio_base64": base64.b64encode(wav_bytes()).decode()}}

    def test_queue_to_completed_contract(self):
        with patch.object(self.client, "request", side_effect=[
            {"id": "job-1", "status": "IN_QUEUE"}, {"id": "job-1", "status": "IN_PROGRESS"}, self.completed
        ]) as request:
            self.assertEqual(self.client.synthesize("Hello", self.state, "sig"), wav_bytes())
            payload = request.call_args_list[0].args[2]
            self.assertEqual(payload["input"]["text"], "Hello")
            self.assertEqual(payload["input"]["mode"], "tts")
            self.assertIn("ref_audio_b64", payload["input"])
            self.assertEqual(json.loads(self.state.read_text())["id"], "job-1")

    def test_resume_does_not_submit(self):
        self.state.write_text(json.dumps({"id": "job-1", "signature": "sig", "status": "IN_PROGRESS"}))
        with patch.object(self.client, "request", return_value=self.completed) as request:
            self.client.synthesize("Hello", self.state, "sig")
            request.assert_called_once_with("GET", "/status/job-1")

    def test_lost_submission_blocks_duplicate(self):
        with patch.object(self.client, "request", side_effect=RuntimeError("Network")):
            with self.assertRaises(RuntimeError):
                self.client.synthesize("Hello", self.state, "sig")
        with patch.object(self.client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                self.client.synthesize("Hello", self.state, "sig")
            request.assert_not_called()

    def test_worker_failure_and_invalid_audio(self):
        for output in ({"ok": False}, {"ok": True, "format": "wav", "audio_base64": "bm90IHdhdg=="}):
            with self.subTest(output=output), patch.object(self.client, "request", return_value={
                "id": "job-1", "status": "COMPLETED", "output": output
            }):
                with self.assertRaises(RuntimeError):
                    self.client.synthesize("Hello", self.state, "sig")

    def test_timeout_preserves_job(self):
        self.client.wait_seconds = 0
        with patch.object(self.client, "request", return_value={"id": "job-1", "status": "IN_QUEUE"}):
            with self.assertRaisesRegex(RuntimeError, "still active"):
                self.client.synthesize("Hello", self.state, "sig")
        self.assertEqual(json.loads(self.state.read_text())["id"], "job-1")

    def test_wav_selected_over_old_mp3(self):
        (self.root / "scene-001.mp3").write_bytes(b"old audio")
        (self.root / "scene-001.wav").write_bytes(wav_bytes())
        (self.root / "scene-001.audio.json").write_text(json.dumps({"file": "scene-001.wav"}))
        self.assertEqual(pipeline.audio_asset(self.root, "scene-001").suffix, ".wav")

    def test_pipeline_saves_runpod_audio_and_reuses_it(self):
        from unittest.mock import Mock
        board = {"style_guide": "Illustration", "aspect_ratio": "9:16", "scenes": [
            {"id": 1, "narration": "Hello", "prompt": "An ocean", "camera_style": "Wide", "mood": "Calm"}]}
        image = Mock()
        image.json.return_value = {"data": [{"b64_json": "aW1hZ2U="}]}
        with patch.object(self.client, "request", return_value=self.completed) as request, patch.object(
            pipeline, "api_post", return_value=image
        ) as api:
            pipeline.generate_assets(board, self.root, "", runpod=self.client)
            pipeline.generate_assets(board, self.root, "", runpod=self.client)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(api.call_count, 1)
            self.assertEqual((self.root / "scene-001.wav").read_bytes(), wav_bytes())
