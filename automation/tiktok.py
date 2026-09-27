"""Renewable TikTok credentials and checkpointed private Direct Post uploads."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import requests


API_ROOT = "https://open.tiktokapis.com"
UPLOAD_DOMAINS = {"tiktokapis.com", "tiktokapis.us"}


class TikTok:
    def __init__(self, credential_state, save, session=None):
        self.session = session or requests.Session()
        self.credential_state = credential_state
        self.save = save
        fallback_token = os.environ["TIKTOK_REFRESH_TOKEN"]
        refresh_token = credential_state.get("refresh_token") or fallback_token
        response = self._refresh(refresh_token)
        error = self._error_code(response)
        if error == "invalid_grant" and refresh_token != fallback_token:
            print("Stored TikTok refresh token was rejected; retrying the configured recovery token…", flush=True)
            response = self._refresh(fallback_token)
        payload = self.check(response, "token refresh")
        self.access_token = payload["access_token"]
        credential_state.update(
            refresh_token=payload["refresh_token"],
            open_id=payload["open_id"],
        )
        save()

    def _refresh(self, refresh_token):
        return self.session.post(
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

    @staticmethod
    def _error_code(response):
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            return None
        return error.get("code") if isinstance(error, dict) else error

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

    @staticmethod
    def _valid_upload_url(upload_url):
        parsed = urlparse(upload_url)
        host = (parsed.hostname or "").casefold()
        valid_host = any(host == domain or host.endswith("." + domain) for domain in UPLOAD_DOMAINS)
        try:
            valid_port = parsed.port in (None, 443)
        except ValueError:
            valid_port = False
        return (parsed.scheme == "https" and valid_host and valid_port
                and parsed.username is None and parsed.password is None)

    def status(self, publish_id):
        response = self.session.post(
            f"{API_ROOT}/v2/post/publish/status/fetch/",
            headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": publish_id},
            timeout=30,
        )
        return self.check(response, "post status")

    def creator_info(self):
        response = self.session.post(
            f"{API_ROOT}/v2/post/publish/creator_info/query/",
            headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8"},
            json={},
            timeout=30,
        )
        return self.check(response, "creator info")

    @staticmethod
    def _utf16_length(value):
        return len(value.encode("utf-16-le")) // 2

    def upload_private(self, path: Path, post: dict, state: dict, save, *, synthetic=False):
        """Direct-post a Short with SELF_ONLY visibility after package approval."""
        path = Path(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if (state.get("started") and not state.get("upload_url") and not state.get("publish_id")
                and os.getenv("TIKTOK_RECOVER_INIT") == "1"):
            for key in ("started", "signature", "privacy_level"):
                state.pop(key, None)
            save()
            print("Reset confirmed pre-upload TikTok Direct Post initiation checkpoint.", flush=True)
        if state.get("signature") and state["signature"] != digest:
            raise ValueError("TikTok video changed after private posting started.")
        if state.get("uploaded"):
            return state["publish_id"]

        caption = post["caption"].strip()
        hashtags = " ".join(
            "#" + "".join(character for character in tag if character.isalnum())
            for tag in post.get("hashtags", []) if any(character.isalnum() for character in tag)
        )
        title = caption + (("\n\n" + hashtags) if hashtags else "")
        if self._utf16_length(title) > 2200:
            raise ValueError("TikTok caption exceeds the 2,200-character limit.")

        creator = self.creator_info()
        if "SELF_ONLY" not in (creator.get("privacy_level_options") or []):
            raise RuntimeError("TikTok does not currently allow private Direct Post for this account.")

        size = path.stat().st_size
        if not state.get("upload_url"):
            if state.get("started"):
                raise RuntimeError("TikTok private-post initiation outcome is unknown; inspect TikTok before retrying.")
            state.update(started=True, signature=digest, privacy_level="SELF_ONLY")
            save()
            response = self.session.post(
                f"{API_ROOT}/v2/post/publish/video/init/",
                headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8"},
                json={
                    "post_info": {
                        "title": title,
                        "privacy_level": "SELF_ONLY",
                        "disable_duet": False,
                        "disable_comment": False,
                        "disable_stitch": False,
                        "video_cover_timestamp_ms": 1000,
                        "is_aigc": bool(synthetic),
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": size,
                        "chunk_size": size,
                        "total_chunk_count": 1,
                    },
                },
                timeout=30,
            )
            try:
                data = self.check(response, "private post initiation")
            except RuntimeError:
                for key in ("started", "signature", "privacy_level"):
                    state.pop(key, None)
                save()
                raise
            upload_url = data["upload_url"]
            if not self._valid_upload_url(upload_url):
                for key in ("started", "signature", "privacy_level"):
                    state.pop(key, None)
                save()
                raise RuntimeError("TikTok returned an unexpected upload host.")
            state.update(publish_id=data["publish_id"], upload_url=upload_url)
            save()

        if state.get("upload_started") and not state.get("uploaded"):
            remote = self.status(state["publish_id"])
            if int(remote.get("uploaded_bytes") or 0) >= size or remote.get("status") in {
                "PROCESSING_DOWNLOAD", "PUBLISH_COMPLETE"
            }:
                state.update(uploaded=True, delivery_status=remote.get("status", "uploaded"))
                state.pop("upload_url", None)
                save()
                return state["publish_id"]
            raise RuntimeError("TikTok private-post byte-upload outcome is unknown; inspect TikTok before retrying.")

        if not self._valid_upload_url(state["upload_url"]):
            raise RuntimeError("TikTok saved an unexpected upload host; no video bytes were sent.")
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
                allow_redirects=False,
            )
        if response.status_code not in (200, 201, 206):
            raise RuntimeError(
                f"TikTok video transfer failed (HTTP {response.status_code}). "
                "Inspect the private posts before retrying."
            )
        state.update(uploaded=True, delivery_status="private_post_submitted")
        state.pop("upload_url", None)
        save()
        return state["publish_id"]

    def upload_draft(self, path: Path, state: dict, save):
        path = Path(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if (state.get("started") and not state.get("upload_url") and not state.get("publish_id")
                and os.getenv("TIKTOK_RECOVER_INIT") == "1"):
            state.pop("started", None)
            state.pop("signature", None)
            save()
            print("Reset confirmed pre-upload TikTok initiation checkpoint.", flush=True)
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
            if not self._valid_upload_url(upload_url):
                state.pop("started", None)
                state.pop("signature", None)
                save()
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

        if not self._valid_upload_url(state["upload_url"]):
            raise RuntimeError("TikTok saved an unexpected upload host; no video bytes were sent.")
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
                allow_redirects=False,
            )
        if response.status_code not in (200, 201, 206):
            raise RuntimeError(
                f"TikTok video transfer failed (HTTP {response.status_code}). "
                "Inspect the TikTok inbox before retrying."
            )
        state.update(uploaded=True, delivery_status="submitted")
        state.pop("upload_url", None)
        save()
        return state["publish_id"]
