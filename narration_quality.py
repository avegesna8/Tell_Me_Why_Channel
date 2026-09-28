"""Local quality checks for a generated continuous narration track."""
from array import array
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import wave


def _tokens(text):
    # IndexTTS phoneme annotations look like <GTA|JH IY1 . T IY1 . EY1>.
    # Whisper should be compared with the visible term, not the ARPABET payload.
    text = re.sub(r"<([^<>|]+)\|[^<>]+>", r"\1", text)
    # Whisper commonly returns initialisms and contractions as one token while
    # authored copy may contain periods or typographic apostrophes.
    text = re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda match: match.group().replace(".", ""), text)
    text = text.replace("’", "'").replace("‘", "'")
    return re.findall(r"[\w']+", text.casefold())


def transcript_match(expected, recognized):
    wanted = _tokens(expected)
    heard = [token for item in recognized for token in _tokens(str(item.get("word", "")))]
    if not wanted:
        raise ValueError("Expected narration is empty.")
    matcher = SequenceMatcher(None, wanted, heard, autojunk=False)
    matches = sum(block.size for block in matcher.get_matching_blocks())
    # Use the larger side as the denominator so omitted and repeated phrases both lower the score.
    return matches / max(len(wanted), len(heard), 1), wanted, heard


def wav_signal(path, silence_db=-42.0):
    """Return conservative speech-track diagnostics without external services."""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                         wav.getframerate(), wav.getnframes())
        if channels < 1 or width != 2 or rate < 8000 or frames <= 0:
            raise ValueError("Narration must be a nonempty 16-bit PCM WAV file.")
        values = array("h")
        values.frombytes(wav.readframes(frames))
    if sys.byteorder != "little":
        values.byteswap()
    mono = []
    for offset in range(0, len(values), channels):
        channel_values = values[offset:offset + channels]
        mono.append(sum(channel_values) / len(channel_values))
    duration = frames / rate
    peak = max((abs(value) for value in mono), default=0)
    rms = math.sqrt(sum(value * value for value in mono) / len(mono)) if mono else 0
    threshold = 32767 * 10 ** (silence_db / 20)
    window = max(1, round(rate * 0.1))
    quiet = []
    for start in range(0, len(mono), window):
        chunk = mono[start:start + window]
        chunk_rms = math.sqrt(sum(value * value for value in chunk) / len(chunk)) if chunk else 0
        quiet.append(chunk_rms < threshold)
    runs, start = [], None
    for index, is_quiet in enumerate(quiet + [False]):
        if is_quiet and start is None:
            start = index
        elif not is_quiet and start is not None:
            runs.append((start * 0.1, min(duration, index * 0.1)))
            start = None
    leading = runs[0][1] if runs and runs[0][0] == 0 else 0.0
    trailing = duration - runs[-1][0] if runs and abs(runs[-1][1] - duration) < 0.11 else 0.0
    internal = max((end - start for start, end in runs if start > 0 and end < duration - 0.1), default=0.0)
    clipped = sum(abs(value) >= 32760 for value in mono) / len(mono) if mono else 0
    return {"duration_seconds": round(duration, 3), "peak_dbfs": round(20 * math.log10(max(peak, 1) / 32767), 2),
            "rms_dbfs": round(20 * math.log10(max(rms, 1) / 32767), 2),
            "leading_silence_seconds": round(leading, 2), "trailing_silence_seconds": round(trailing, 2),
            "longest_internal_silence_seconds": round(internal, 2), "clipped_sample_fraction": round(clipped, 6)}


def inspect(audio, expected, model="small", *, transcriber=None):
    from narration_alignment import transcribe
    audio = Path(audio)
    signal = wav_signal(audio)
    words = (transcriber or transcribe)(audio, model)
    matched, wanted, heard = transcript_match(expected, words)
    comparison = SequenceMatcher(None, wanted, heard, autojunk=False)
    mismatch_ops = [(tag, a, b, c, d) for tag, a, b, c, d in comparison.get_opcodes() if tag != "equal"]
    mismatches = [{"expected": " ".join(wanted[a:b]), "recognized": " ".join(heard[c:d])}
                  for _tag, a, b, c, d in mismatch_ops][:12]
    longest_expected_mismatch = max((b - a for _tag, a, b, _c, _d in mismatch_ops), default=0)
    warnings, failures = [], []
    if matched < 0.85:
        failures.append(f"Whisper matched only {matched:.0%} of the intended narration")
    elif matched < 0.94:
        warnings.append(f"Whisper matched {matched:.0%}; review names, acronyms, and isolated word differences")
    if longest_expected_mismatch >= 6:
        failures.append(f"a phrase of {longest_expected_mismatch} intended words was missing or garbled")
    if signal["leading_silence_seconds"] > 1.5:
        warnings.append("long silence at the beginning")
    if signal["trailing_silence_seconds"] > 2.0:
        warnings.append("long silence at the end")
    if signal["longest_internal_silence_seconds"] > 2.5:
        failures.append("an unusually long internal silence was detected")
    if signal["clipped_sample_fraction"] > 0.005:
        failures.append("the narration contains substantial clipping")
    if signal["rms_dbfs"] < -38:
        failures.append("the narration is nearly silent")
    return {"passed": not failures, "matched_fraction": round(matched, 4), "signal": signal,
            "warnings": warnings, "failures": failures, "mismatches": mismatches,
            "expected_word_count": len(wanted), "recognized_word_count": len(heard), "words": words,
            "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "whisper_model": model,
            "policy_version": 3}


def apply_final_attempt_policy(report):
    """Accept a marginal final transcript only when every structural and signal check passed."""
    transcript_failures = [failure for failure in report.get("failures", [])
                           if failure.startswith("Whisper matched only ")]
    other_failures = [failure for failure in report.get("failures", [])
                      if not failure.startswith("Whisper matched only ")]
    if (report.get("attempt", 1) >= 3 and report.get("matched_fraction", 0) >= .80
            and transcript_failures and not other_failures):
        report["failures"] = []
        report["passed"] = True
        warning = (f"Final attempt matched {report['matched_fraction']:.0%}; structural and signal checks passed, "
                   "but review names, acronyms, and isolated word differences")
        if warning not in report.setdefault("warnings", []):
            report["warnings"].append(warning)
    return report


def cached_report(path, audio, expected, model):
    path = Path(path)
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    digest = hashlib.sha256(Path(audio).read_bytes()).hexdigest()
    expected_digest = hashlib.sha256(expected.encode()).hexdigest()
    if not (report.get("audio_sha256") == digest and report.get("expected_sha256") == expected_digest
            and report.get("whisper_model") == model):
        return None
    policy_version = report.get("policy_version", 1)
    if policy_version < 2:
        transcript_failure = [failure for failure in report.get("failures", [])
                              if failure.startswith("Whisper matched only ")]
        other_failures = [failure for failure in report.get("failures", [])
                          if not failure.startswith("Whisper matched only ")]
        if report.get("matched_fraction", 0) >= .85 and transcript_failure and not other_failures:
            report["failures"] = []
            report["passed"] = True
            warning = (f"Whisper matched {report['matched_fraction']:.0%}; review names, acronyms, "
                       "and isolated word differences")
            if warning not in report.setdefault("warnings", []):
                report["warnings"].append(warning)
    if policy_version < 3:
        apply_final_attempt_policy(report)
        report["policy_version"] = 3
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report
