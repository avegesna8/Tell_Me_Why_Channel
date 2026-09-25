"""Client for indextts-2-youtube/handler.py on a queue-based RunPod endpoint."""
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import time
import wave

import requests


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


class RunPodTTS:
    def __init__(self, endpoint, reference, *, wait_seconds=900, poll_seconds=5):
        endpoint = endpoint.strip()
        match = re.fullmatch(r"https://api\.runpod\.ai/v2/([A-Za-z0-9_-]+)(?:/(?:run|runsync))?/?", endpoint)
        self.endpoint = match.group(1) if match else endpoint
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.endpoint):
            raise ValueError("Set RUNPOD_ENDPOINT_ID to your endpoint ID or api.runpod.ai/v2 endpoint URL.")
        key = os.getenv("RUNPOD_API_KEY", "").strip()
        if not key:
            raise ValueError("Set RUNPOD_API_KEY in .env.")
        if not reference or not Path(reference).is_file():
            raise ValueError("Set RUNPOD_REFERENCE_AUDIO or --reference-audio to a real reference speech file.")
        raw = Path(reference).read_bytes()
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise ValueError("Reference audio must be nonempty and under 5 MB; use a short, clean speech sample.")
        self.reference_b64 = base64.b64encode(raw).decode()
        self.reference_hash = hashlib.sha256(raw).hexdigest()
        self.base = f"https://api.runpod.ai/v2/{self.endpoint}"
        self.headers = {"Authorization": f"Bearer {key}"}
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.model_version = os.getenv("RUNPOD_MODEL_VERSION", "2")
        self.delivery = {}
        if self.model_version not in ("2", "2.5"):
            raise ValueError("RUNPOD_MODEL_VERSION must be 2 or 2.5.")
        if self.model_version == "2.5":
            factor = float(os.getenv("RUNPOD_DURATION_FACTOR", "1.11"))
            strength = float(os.getenv("RUNPOD_EMOTION_STRENGTH", "0.5"))
            if not math.isfinite(factor) or not 0.5 <= factor <= 2 or not math.isfinite(strength) or not 0 <= strength <= 1:
                raise ValueError("RunPod duration factor must be 0.5–2; emotion strength must be 0–1.")
            self.delivery = {"duration_factor": factor, "lang": "EN"}
            emotion = os.getenv("RUNPOD_EMOTION", "").strip()
            if emotion:
                self.delivery.update(emotion=emotion, emotion_strength=strength)

    def fingerprint_data(self, text):
        value = {"provider": "runpod", "endpoint": self.endpoint, "text": text,
                "reference_sha256": self.reference_hash, "contract": "indextts-2-youtube-v1"}
        if self.model_version == "2.5":
            value.update(model_version="2.5", delivery=self.delivery)
        return value

    def request(self, method, route, payload=None):
        try:
            response = requests.request(method, self.base + route, headers=self.headers,
                                        json=payload, timeout=(15, 60))
        except requests.RequestException as exc:
            raise RuntimeError("RunPod network request failed. Saved job state is preserved; no automatic resubmission.") from exc
        if not response.ok:
            raise RuntimeError(f"RunPod HTTP {response.status_code} on {route}. "
                               "Check endpoint ID, API key, quota and job status in RunPod console.")
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("RunPod returned an unexpected response.")
        return value

    def synthesize(self, text, state_path, signature):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        if state and state.get("signature") != signature:
            if state.get("status") not in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                raise RuntimeError(f"An older RunPod job may still be active. Check it before removing {state_path} to submit changed narration.")
            state = None
        if state:
            job_id = state.get("id")
            if not job_id:
                raise RuntimeError(f"Previous submission outcome is unknown. Check RunPod Requests before removing {state_path} and retrying.")
            print(f"Resuming RunPod job {job_id}", flush=True)
            result = self.request("GET", f"/status/{job_id}")
        else:
            # Persist intent before POST: a lost response must not silently duplicate a paid job.
            save(state_path, {"signature": signature, "endpoint": self.endpoint, "status": "SUBMITTING"})
            print("Submitting IndexTTS-2 narration to RunPod…", flush=True)
            result = self.request("POST", "/run", {"input": {
                "mode": "tts", "text": text, "ref_audio_b64": self.reference_b64, **self.delivery}})
            job_id = result.get("id")
            if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                raise RuntimeError("RunPod returned no valid job ID; inspect Requests before retrying.")
        started = time.monotonic()
        while True:
            status = result.get("status", "UNKNOWN")
            save(state_path, {"signature": signature, "endpoint": self.endpoint, "id": job_id, "status": status})
            print(f"RunPod {job_id}: {status} ({int(time.monotonic()-started)}s)", flush=True)
            if status == "COMPLETED":
                output = result.get("output")
                if not isinstance(output, dict) or output.get("ok") is not True:
                    raise RuntimeError(f"RunPod worker reported synthesis failure. Check logs for job {job_id}.")
                if output.get("format") != "wav":
                    raise RuntimeError("RunPod worker must return format=wav and audio_base64.")
                if self.model_version == "2.5" and output.get("model_version") != "2.5":
                    raise RuntimeError("Endpoint is not running the expected IndexTTS-2.5 worker. Deploy it before enabling RUNPOD_MODEL_VERSION=2.5.")
                try:
                    data = base64.b64decode(output["audio_base64"], validate=True)
                    with wave.open(io.BytesIO(data), "rb") as wav:
                        if wav.getnframes() <= 0:
                            raise ValueError("Empty WAV")
                except (KeyError, TypeError, ValueError, wave.Error, EOFError) as exc:
                    raise RuntimeError("RunPod returned invalid or empty WAV audio.") from exc
                return data
            if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                raise RuntimeError(f"RunPod job {job_id} {status}. Check worker logs; remove {state_path} after resolving the cause to submit again.")
            if status not in ("IN_QUEUE", "IN_PROGRESS"):
                raise RuntimeError(f"Unexpected RunPod job status: {status}")
            if time.monotonic() - started >= self.wait_seconds:
                raise RuntimeError("Stopped waiting for RunPod. The remote job is still active; rerun to resume polling its saved ID.")
            time.sleep(self.poll_seconds)
            result = self.request("GET", f"/status/{job_id}")
