"""Local OCR checks that keep generated scene art free of accidental text."""
import csv
import hashlib
import io
import json
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


def inspect(path):
    path = Path(path)
    detected = ocr_image(path)
    return {"passed": not detected, "detected_text": detected,
            "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


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
