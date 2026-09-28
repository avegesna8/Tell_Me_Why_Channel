"""Local OCR checks that keep generated scene art free of accidental text."""
import csv
import hashlib
import io
import json
from functools import lru_cache
from pathlib import Path
import re
import subprocess


def ocr_image(path, *, executable="tesseract", minimum_confidence=72):
    result = subprocess.run([executable, str(path), "stdout", "--psm", "11", "tsv"],
                            capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown OCR error"
        raise RuntimeError(f"Tesseract could not inspect {Path(path).name}: {detail}")
    found = []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        text = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf", -1))
        except (TypeError, ValueError):
            continue
        compact = re.sub(r"[^A-Za-z0-9]", "", text)
        if confidence >= minimum_confidence and len(compact) >= 3 and re.search(r"[A-Za-z]", compact):
            found.append({"text": text, "confidence": round(confidence, 1)})
    return found


@lru_cache(maxsize=1)
def english_words():
    candidates = (Path("/usr/share/dict/words"), Path("/usr/share/dict/american-english"))
    for path in candidates:
        if path.is_file():
            return {word.strip().casefold() for word in path.read_text(errors="ignore").splitlines()
                    if word.strip().isalpha()}
    raise RuntimeError("Install an English word list (wamerican) for image spelling checks.")


def suspicious_text(detected, allowed_text="", dictionary=None):
    """Return OCR words that look misspelled rather than merely readable."""
    dictionary = english_words() if dictionary is None else {word.casefold() for word in dictionary}
    allowed = {word.casefold() for word in re.findall(r"[A-Za-z0-9]+", allowed_text)}
    suspicious = []
    for item in detected:
        raw = str(item.get("text", ""))
        compact = re.sub(r"[^A-Za-z]", "", raw)
        word = compact.casefold()
        if len(word) < 4 or word in dictionary or word in allowed:
            continue
        components = [part.casefold() for part in re.findall(r"[A-Za-z]+", raw)]
        if len(components) > 1 and all(part in dictionary or part in allowed for part in components):
            continue
        suspicious.append(item)
    return suspicious


def inspect(path, allowed_text=""):
    path = Path(path)
    detected = ocr_image(path)
    suspicious = suspicious_text(detected, allowed_text)
    return {"passed": not suspicious, "detected_text": detected, "suspicious_text": suspicious,
            "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "policy_version": 2}


def cached_scene(report_path, stem, path, signature):
    report_path, path = Path(report_path), Path(path)
    if not report_path.exists():
        return None
    report = json.loads(report_path.read_text())
    scene = report.get("scenes", {}).get(stem)
    if not isinstance(scene, dict):
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return scene if scene.get("image_sha256") == digest and scene.get("prompt_signature") == signature else None
