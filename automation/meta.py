"""Checkpointed unpublished Facebook Page video and Reel-draft uploads."""
from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

import requests


GRAPH_ROOT = "https://graph.facebook.com/v26.0"
VIDEO_ROOT = "https://graph-video.facebook.com/v26.0"
ALLOWED_UPLOAD_HOSTS = {"rupload.facebook.com", "graph-video.facebook.com"}


class Meta:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.page_id = os.environ["META_PAGE_ID"]
        self.session.headers["Authorization"] = "Bearer " + os.environ["META_PAGE_ACCESS_TOKEN"]

    @staticmethod
    def check(response, label):
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Meta {label} returned an unreadable response.") from exc
        if not response.ok or payload.get("error"):
            error = payload.get("error", {})
            code = error.get("code") if isinstance(error, dict) else None
            kind = error.get("type") if isinstance(error, dict) else None
            safe = ", ".join(str(value)[:80] for value in (code, kind) if value)
            raise RuntimeError(
                f"Meta {label} failed: HTTP {response.status_code}"
                + (f" ({safe})" if safe else "")
                + ". Check Page permissions and API status."
            )
        return payload

    @staticmethod
    def _signature(path, metadata):
        with Path(path).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return hashlib.sha256((digest + str(metadata)).encode()).hexdigest()

    def upload_unpublished_video(self, path, metadata, state, save, thumbnail=None):
        signature_metadata = dict(metadata)
        if thumbnail:
            signature_metadata['thumbnail_sha256'] = hashlib.sha256(Path(thumbnail).read_bytes()).hexdigest()
        signature = self._signature(path, signature_metadata)
        if state.get("signature") and state["signature"] != signature:
            raise ValueError("Facebook long video or metadata changed after upload started.")
        if state.get("id"):
            return state["id"]
        if state.get("started"):
            raise RuntimeError("Facebook long-video upload outcome is unknown; inspect Meta Business Suite before retrying.")
        state.update(started=True, signature=signature)
        save()
        with ExitStack() as stack:
            video = stack.enter_context(Path(path).open("rb"))
            files = {"source": ("video.mp4", video, "video/mp4")}
            if thumbnail:
                cover = stack.enter_context(Path(thumbnail).open("rb"))
                files["thumb"] = ("thumbnail.jpg", cover, "image/jpeg")
            response = self.session.post(
                f"{VIDEO_ROOT}/{self.page_id}/videos",
                data={
                    "published": "false",
                    "unpublished_content_type": "DRAFT",
                    "title": metadata["title"],
                    "description": metadata["description"],
                },
                files=files,
                timeout=(15, 1800),
            )
        try:
            payload = self.check(response, "unpublished video upload")
        except RuntimeError:
            state.pop("started", None)
            state.pop("signature", None)
            save()
            raise
        state["id"] = str(payload["id"])
        save()
        return state["id"]

    def upload_reel_draft(self, path, metadata, state, save):
        signature = self._signature(path, metadata)
        if state.get("signature") and state["signature"] != signature:
            raise ValueError("Facebook Reel or metadata changed after upload started.")
        if state.get("id") and state.get("finished"):
            return state["id"]
        size = Path(path).stat().st_size
        if not state.get("upload_url"):
            if state.get("started"):
                raise RuntimeError("Facebook Reel initiation outcome is unknown; inspect Meta Business Suite before retrying.")
            state.update(started=True, signature=signature)
            save()
            response = self.session.post(
                f"{GRAPH_ROOT}/{self.page_id}/video_reels",
                data={"upload_phase": "start"},
                timeout=60,
            )
            try:
                payload = self.check(response, "Reel draft initiation")
            except RuntimeError:
                state.pop("started", None)
                state.pop("signature", None)
                save()
                raise
            upload_url = payload["upload_url"]
            parsed = urlparse(upload_url)
            if parsed.scheme != "https" or parsed.hostname not in ALLOWED_UPLOAD_HOSTS:
                raise RuntimeError("Meta returned an unexpected Reel upload host.")
            state.update(id=str(payload["video_id"]), upload_url=upload_url)
            save()

        if state.get("upload_started") and not state.get("uploaded"):
            raise RuntimeError("Facebook Reel byte-upload outcome is unknown; inspect Meta Business Suite before retrying.")
        if not state.get("uploaded"):
            state["upload_started"] = True
            save()
            with Path(path).open("rb") as video:
                response = self.session.post(
                    state["upload_url"],
                    headers={"offset": "0", "file_size": str(size)},
                    data=video,
                    timeout=(15, 900),
                )
            self.check(response, "Reel transfer")
            state["uploaded"] = True
            state.pop("upload_url", None)
            save()

        response = self.session.post(
            f"{GRAPH_ROOT}/{self.page_id}/video_reels",
            data={
                "upload_phase": "finish",
                "video_id": state["id"],
                "video_state": "DRAFT",
                "description": metadata["description"],
            },
            timeout=60,
        )
        self.check(response, "Reel draft finish")
        state["finished"] = True
        save()
        return state["id"]

    def business_suite_url(self):
        return f"https://business.facebook.com/latest/content?asset_id={self.page_id}"
