"""Optional, budget-capped Runway image-to-video scene animation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import requests

API_ROOT = "https://api.dev.runwayml.com/v1"
API_VERSION = "2024-11-06"
TERMINAL_FAILURES = {"FAILED", "CANCELED", "CANCELLED"}
SUCCESS = "SUCCEEDED"


class RunwayInsufficientFundsError(RuntimeError):
    """A non-retryable Runway billing failure that must not silently fall back."""


def enabled():
    return os.getenv("ENABLE_ANIMATION", "0").strip().lower() in {"1", "true", "yes", "on"}


def selected_scene_ids(board, limit):
    scenes = board["scenes"]
    if limit <= 0 or not scenes:
        return []
    limit = min(limit, len(scenes))
    if limit == 1:
        return [scenes[len(scenes) // 2]["id"]]
    indexes = []
    for slot in range(1, limit + 1):
        index = round(slot * (len(scenes) - 1) / (limit + 1))
        if index not in indexes:
            indexes.append(index)
    for index in range(len(scenes)):
        if len(indexes) == limit:
            break
        if index not in indexes:
            indexes.append(index)
    return [scenes[index]["id"] for index in sorted(indexes[:limit])]


def _save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def _response_json(response, label):
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        code = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
        detail = json.dumps(code, sort_keys=True) if isinstance(code, (dict, list)) else str(code)
        normalized = detail.lower().replace("_", " ").replace("-", " ")
        funds_markers = (
            "insufficient funds", "insufficient balance", "insufficient credits",
            "credit balance", "credits exhausted", "balance exhausted",
            "payment required", "billing limit", "spend limit", "quota exceeded",
        )
        if response.status_code == 402 or any(marker in normalized for marker in funds_markers):
            raise RunwayInsufficientFundsError(
                "Runway has insufficient funds or available credits. Add credits and check the "
                "workspace spending limit in Runway before rerunning."
            )
        raise RuntimeError(f"Runway {label} failed: {detail[:500]}")
    return payload


def _image_data_uri(path):
    data = path.read_bytes()
    if not data:
        raise ValueError(f"Animation source image is empty: {path}")
    if len(data) > 5 * 1024 * 1024:
        raise ValueError(f"Animation source image exceeds Runway's 5MB data-URI limit: {path}")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _prompt(scene):
    return (
        "Animate this exact illustrated frame with restrained, natural motion while preserving the "
        "character design, composition, colors, clothing, facial identity, and drawing style. "
        "Use subtle character gestures, environmental motion, and a gentle cinematic camera move "
        "that support the explanation. Keep motion physically coherent. Do not add text, logos, "
        "new characters, scene cuts, speech, lip-sync, or unrelated objects. Scene intent: "
        + scene["prompt"]
    )[:1000]


def _task(session, scene, image, assets, signature, attempt, model, duration, ratio, timeout):
    job = assets / f"scene-{scene['id']:03}.animation-attempt-{attempt}.json"
    record = json.loads(job.read_text()) if job.exists() else {}
    task_id = record.get("task_id") if record.get("signature") == signature else None
    headers = {
        "Authorization": "Bearer " + os.environ["RUNWAYML_API_SECRET"],
        "Content-Type": "application/json",
        "X-Runway-Version": API_VERSION,
    }
    if not task_id:
        response = session.post(
            API_ROOT + "/image_to_video",
            headers=headers,
            json={
                "model": model,
                "promptImage": _image_data_uri(image),
                "promptText": _prompt(scene),
                "ratio": ratio,
                "duration": duration,
            },
            timeout=(15, 120),
        )
        payload = _response_json(response, "task creation")
        task_id = payload.get("id")
        if not task_id:
            raise RuntimeError("Runway task creation returned no task ID.")
        _save_json(job, {"signature": signature, "task_id": task_id, "attempt": attempt})
    deadline = time.monotonic() + timeout
    while True:
        response = session.get(API_ROOT + f"/tasks/{task_id}", headers=headers, timeout=(15, 60))
        payload = _response_json(response, "task status")
        status = payload.get("status")
        if status == SUCCESS:
            output = payload.get("output") or []
            if not output:
                raise RuntimeError("Runway completed without a video URL.")
            return output[0]
        if status in TERMINAL_FAILURES:
            _save_json(job, {**record, "signature": signature, "task_id": task_id,
                             "attempt": attempt, "status": status,
                             "failure_code": payload.get("failureCode")})
            return None
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Runway task {task_id} did not finish within {timeout} seconds.")
        time.sleep(5)


def generate_animations(board, directory, kind, session=None):
    """Generate optional clips; any provider failure falls back to the original still."""
    if not enabled():
        return {"enabled": False, "selected": [], "animated": [], "fallback": []}
    secret = os.getenv("RUNWAYML_API_SECRET", "").strip()
    if not secret:
        raise ValueError("ENABLE_ANIMATION is on but RUNWAYML_API_SECRET is missing.")
    if kind not in ("long", "short"):
        raise ValueError("Animation kind must be long or short.")
    assets = Path(directory) / "assets"
    limit = int(os.getenv("ANIMATION_LONG_SCENES", "3") if kind == "long"
                else os.getenv("ANIMATION_SHORT_SCENES", "1"))
    if not 0 <= limit <= 5:
        raise ValueError("Animation scene limits must be between 0 and 5.")
    model = os.getenv("RUNWAY_ANIMATION_MODEL", "gen4_turbo")
    duration = int(os.getenv("RUNWAY_ANIMATION_SECONDS", "5"))
    if model == "gen4_turbo" and duration not in (5, 10):
        raise ValueError("Runway Gen-4 Turbo animation duration must be 5 or 10 seconds.")
    selected = selected_scene_ids(board, limit)
    report = {"enabled": True, "provider": "runway", "model": model, "seconds": duration,
              "max_attempts": 2, "selected": selected, "animated": [], "fallback": []}
    client = session or requests.Session()
    scenes = {scene["id"]: scene for scene in board["scenes"]}
    for scene_id in selected:
        scene = scenes[scene_id]
        stem = f"scene-{scene_id:03}"
        image = assets / f"{stem}.png"
        output = assets / f"{stem}.animation.mp4"
        stamp = assets / f"{stem}.animation.json"
        signature = hashlib.sha256(
            image.read_bytes() + _prompt(scene).encode() + model.encode() + str(duration).encode()
        ).hexdigest()
        saved = json.loads(stamp.read_text()) if stamp.exists() else {}
        if output.exists() and output.stat().st_size and saved.get("signature") == signature:
            report["animated"].append(scene_id)
            continue
        try:
            url = None
            for attempt in (1, 2):
                url = _task(client, scene, image, assets, signature, attempt, model, duration,
                            '1280:720' if board['aspect_ratio'] == '16:9' else '720:1280', 600)
                if url:
                    break
            if not url:
                report["fallback"].append({"scene": scene_id, "reason": "generation_failed"})
                continue
            if not isinstance(url, str):
                raise RuntimeError("Runway returned an invalid output URL.")
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise RuntimeError("Runway returned an invalid output URL.")
            response = client.get(url, timeout=(15, 180))
            if not response.ok or not response.content:
                raise RuntimeError(f"Runway video download failed: HTTP {response.status_code}")
            temporary = output.with_suffix(".partial.mp4")
            temporary.write_bytes(response.content)
            temporary.replace(output)
            _save_json(stamp, {"signature": signature, "file": output.name, "task_output": "downloaded"})
            report["animated"].append(scene_id)
        except RunwayInsufficientFundsError:
            raise
        except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
            print(f"Animation fallback for {stem}: {exc}", flush=True)
            report["fallback"].append({"scene": scene_id, "reason": str(exc)[:300]})
    _save_json(Path(directory) / "animation-manifest.json", report)
    return report
