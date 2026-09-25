# app.py
import io
import os
import json
import time
import logging
import requests
import zipfile, tempfile, pathlib
from typing import Optional, Literal, Dict, Any
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator

# =========================
# Config
# =========================
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE = os.getenv("SILICONFLOW_BASE", "https://api.siliconflow.com")
INDEXTTS_MODEL = os.getenv("INDEXTTS_MODEL", "IndexTeam/IndexTTS-2")  # allow overriding if needed

if not SILICONFLOW_API_KEY:
    raise RuntimeError("Missing env var SILICONFLOW_API_KEY")

HEADERS_JSON = {
    "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
    "Content-Type": "application/json",
}
HEADERS_MULTIPART = {
    "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
}
TIMEOUT = (10, 120)  # (connect, read)

# =========================
# App & Logger
# =========================
app = FastAPI(title="IndexTTS-2 Gateway", version="1.1.0")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("indextts2-gw")

# =========================
# Schemas
# =========================
class SynthesizeIn(BaseModel):
    # match SiliconFlow's API fields directly
    input: str = Field(..., description="Input text to speak")   # was `text`
    voice: Optional[str] = None                                  # built-in voice or URI
    response_format: Literal["wav", "mp3"] = "wav"
    sample_rate: int = 24000
    stream: bool = False
    speed: Optional[float] = None
    gain: Optional[float] = None
    seed: Optional[int] = None
    duration_tokens: Optional[int] = None

    @validator("input")
    def nonempty(cls, v):
        if not v.strip():
            raise ValueError("input cannot be empty")
        return v

# =========================
# Helpers
# =========================
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac"}
MAX_ZIP_BYTES = 200 * 1024 * 1024  # 200 MB guard
MAX_ZIP_FILES = 50                 # avoid gigantic archives

def headers(h: dict) -> dict:
    # tiny helper to clone headers (requests may mutate dicts on redirects)
    return {k: v for k, v in h.items()}

def _size_bytes_utf8(s: str) -> int:
    return len(s.encode("utf-8"))

def _is_safe_member(member: zipfile.ZipInfo) -> bool:
    # prevent path traversal / absolute paths
    p = pathlib.Path(member.filename)
    return not (p.is_absolute() or ".." in p.parts)

def _pick_first_audio_from_zip(zip_fp) -> tuple[str, bytes, str]:
    """
    Returns (filename, audio_bytes, mime) from the first audio file in the ZIP.
    Raises ValueError if not found or unsafe.
    """
    with zipfile.ZipFile(zip_fp) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise ValueError(f"Too many files in zip (> {MAX_ZIP_FILES}).")

        total_uncomp = 0
        candidates = []
        for info in infos:
            if not _is_safe_member(info):
                continue
            total_uncomp += info.file_size
            if total_uncomp > MAX_ZIP_BYTES:
                raise ValueError("ZIP content too large.")
            ext = pathlib.Path(info.filename).suffix.lower()
            if ext in AUDIO_EXTS and not info.is_dir():
                candidates.append(info)

        if not candidates:
            raise ValueError("No supported audio found in ZIP (need .wav, .mp3, .m4a, .flac).")

        info = candidates[0]
        with zf.open(info, "r") as f:
            data = f.read()

        ext = pathlib.Path(info.filename).suffix.lower()
        mime = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
        }.get(ext, "application/octet-stream")

        safe_name = pathlib.Path(info.filename).name
        return safe_name, data, mime

# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return {"ok": True, "model": INDEXTTS_MODEL, "base": SILICONFLOW_BASE}

@app.post("/voices")
def upload_voice(
    file: UploadFile = File(..., description="Audio (.wav/.mp3/.m4a/.flac) OR a .zip containing one audio file"),
    custom_name: str = Form("my-voice"),
    ref_text: str = Form("This is a short reference to learn my voice."),
):
    """
    Accept a raw audio file or a .zip containing a single audio file.
    Unzips safely, picks the first supported audio, and forwards to SiliconFlow.
    """

    raw = file.file.read()
    filename_lower = (file.filename or "upload").lower()
    is_zip = filename_lower.endswith(".zip") or (file.content_type or "").endswith("zip")

    if is_zip:
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as tf:
            tf.write(raw)
            tf.seek(0)
            try:
                audio_name, audio_bytes, mime = _pick_first_audio_from_zip(tf)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        upload_filename = audio_name
        upload_bytes = audio_bytes
        upload_mime = mime
    else:
        upload_filename = file.filename or "voice.wav"
        upload_bytes = raw
        ext = pathlib.Path(upload_filename).suffix.lower()
        upload_mime = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
        }.get(ext, file.content_type or "application/octet-stream")

    sf_url = f"{SILICONFLOW_BASE}/v1/uploads/audio/voice"
    files = {
        "file": (upload_filename, io.BytesIO(upload_bytes), upload_mime),
    }
    data = {"model": INDEXTTS_MODEL, "customName": custom_name, "text": ref_text}

    r = requests.post(sf_url, headers=headers(HEADERS_MULTIPART), files=files, data=data, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    try:
        payload = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"Unexpected response: {r.text[:500]}")

    voice_uri = payload.get("uri")
    if not voice_uri:
        raise HTTPException(status_code=502, detail=f"No voice URI in response: {payload}")
    return {"voice_uri": voice_uri, "name": custom_name}

@app.get("/voices")
def list_voices():
    """
    List uploaded voices (URIs) for this account.
    """
    url = f"{SILICONFLOW_BASE}/v1/audio/voice/list"
    r = requests.get(url, headers=headers(HEADERS_JSON), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

@app.post("/synthesize")
def synth(body: SynthesizeIn = Body(...)):
    """
    Synthesize audio with IndexTTS-2. Returns audio bytes (wav/mp3).
    """
    input_bytes = len(body.input.encode("utf-8"))
    approx_cost_usd = input_bytes / 1_000_000 * 7.15
    log.info(
        "synthesize bytes=%d (~$%.6f) voice=%s fmt=%s sr=%d stream=%s",
        input_bytes, approx_cost_usd, body.voice,
        body.response_format, body.sample_rate, body.stream
    )

    payload = {
        "model": INDEXTTS_MODEL,
        "input": body.input,   # provider expects this
        "response_format": body.response_format,
        "sample_rate": body.sample_rate,
        "stream": body.stream,
    }

    # voice handling
    if body.voice:
        payload["voice"] = body.voice  # preset or uploaded speech: URI

    # optional controls
    if body.speed is not None:
        payload["speed"] = body.speed
    if body.gain is not None:
        payload["gain"] = body.gain
    if body.seed is not None:
        payload["seed"] = body.seed
    if body.duration_tokens is not None:
        payload["duration_tokens"] = body.duration_tokens

    url = f"{SILICONFLOW_BASE}/v1/audio/speech"
    r = requests.post(url, headers=headers(HEADERS_JSON),
                      data=json.dumps(payload), timeout=TIMEOUT, stream=True)

    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        log.error("Provider error %s: %s", r.status_code, detail)
        raise HTTPException(status_code=r.status_code, detail=detail)

    media_type = "audio/wav" if body.response_format == "wav" else "audio/mpeg"
    filename = f"speech-{int(time.time())}.{body.response_format}"
    return StreamingResponse(
        r.iter_content(chunk_size=8192),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )


@app.post("/voices/delete")
def delete_voice(payload: Dict[str, str]):
    """
    Delete a custom voice by URI.
    """
    uri = payload.get("uri")
    if not uri:
        raise HTTPException(status_code=400, detail="Missing 'uri' in body")
    url = f"{SILICONFLOW_BASE}/v1/audio/voice/deletions"
    r = requests.post(url, headers=headers(HEADERS_JSON), data=json.dumps({"uri": uri}), timeout=TIMEOUT)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

