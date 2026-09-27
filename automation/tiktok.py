"""Renewable TikTok Sandbox credentials and checkpointed inbox-draft uploads."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import requests


API_ROOT = "https://open.tiktokapis.com"
UPLOAD_HOST = "open-upload.tiktokapis.com"


class TikTok:
    def __init__(self, credential_state, save, session=None):
        self.session = session or requests.Session()
        self.credential_state = credential_state
        self.save = save
        refresh_token = credential_state.get("refresh_token") or os.environ["TIKTOK_REFRESH_TOKEN"]
        response = self.session.post(
            f"{API_ROOT}/v2/oauth/token/",
            data={
                "client_key": os.environ["TIKTOK_CLIENT_KEY"],
                "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        payload = self.check(response, "token refresh")
        self.access_token = payload["access_token"]
        credential_state.update(
            refresh_token=payload["refresh_token"],
            open_id=payload["open_id"],
        )
        save()

    @staticmethod
    def check(response, label):
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"TikTok {label} returned an unreadable response.") from exc
        error = payload.get("error")
        error_code = error.get("code") if isinstance(error, dict) else error
        if not response.ok or error_code not in (None, "", "ok"):
            safe = str(error_code or f"HTTP {response.status_code}")[:100]
            raise RuntimeError(f"TikTok {label} failed ({safe}). Check credentials, scope and API status.")
        return payload.get("data", payload)

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def status(self, publish_id):
        response = self.session.post(
            f"{API_ROOT}/v2/post/publish/status/fetch/",
            headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": publish_id},
            timeout=30,
        )
        return self.check(response, "draft status")

    def upload_draft(self, path: Path, state: dict, save):
        path = Path(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if state.get("signature") and state["signature"] != digest:
            raise ValueError("TikTok draft file changed after upload started.")
        if state.get("uploaded"):
            return state["publish_id"]

        size = path.stat().st_size
        if not state.get("upload_url"):
            if state.get("started"):
                raise RuntimeError("TikTok draft initiation outcome is unknown; inspect the TikTok inbox before retrying.")
            state.update(started=True, signature=digest)
            save()
            response = self.session.post(
                f"{API_ROOT}/v2/post/publish/inbox/video/init/",
                headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8"},
                json={"source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,
                    "total_chunk_count": 1,
                }},
                timeout=30,
            )
            try:
                data = self.check(response, "draft initiation")
            except RuntimeError:
                state.pop("started", None)
                state.pop("signature", None)
                save()
                raise
            upload_url = data["upload_url"]
            parsed = urlparse(upload_url)
            if parsed.scheme != "https" or parsed.hostname != UPLOAD_HOST:
                raise RuntimeError("TikTok returned an unexpected upload host.")
            state.update(publish_id=data["publish_id"], upload_url=upload_url)
            save()

        if state.get("upload_started") and not state.get("uploaded"):
            remote = self.status(state["publish_id"])
            if int(remote.get("uploaded_bytes") or 0) >= size or remote.get("status") in {
                "PROCESSING_DOWNLOAD", "SEND_TO_USER", "PUBLISH_COMPLETE"
            }:
                state.update(uploaded=True, delivery_status=remote.get("status", "uploaded"))
                state.pop("upload_url", None)
                save()
                return state["publish_id"]
            raise RuntimeError("TikTok draft byte-upload outcome is unknown; inspect the TikTok inbox before retrying.")

        state["upload_started"] = True
        save()
        with path.open("rb") as video:
            response = self.session.put(
                state["upload_url"],
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                data=video,
                timeout=(15, 600),
            )
        if not response.ok:
            raise RuntimeError(
                f"TikTok video transfer failed (HTTP {response.status_code}). "
                "Inspect the TikTok inbox before retrying."
            )
        state.update(uploaded=True, delivery_status="submitted")
        state.pop("upload_url", None)
        save()
        return state["publish_id"]
