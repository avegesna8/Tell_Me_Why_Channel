"""Prepare written narration for speech without changing captions or the script."""
import re

ARPABET = set("AA AE AH AO AW AY B CH D DH EH ER EY F G HH IH IY JH K L M N NG OW OY P R S SH T TH UH UW V W Y Z ZH".split())

def _number(value):
    n = int(value)
    ones = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    if n < 20: return ones[n]
    if n < 100: return tens[n // 10] + ("-" + ones[n % 10] if n % 10 else "")
    if n < 1000: return ones[n // 100] + " hundred" + (" " + _number(n % 100) if n % 100 else "")
    if n < 10000: return _number(n // 1000) + " thousand" + (" " + _number(n % 1000) if n % 1000 else "")
    return value

def normalize_spoken(text):
    """Expand common ambiguous written forms conservatively for English TTS."""
    changes = []
    def replace(pattern, make):
        nonlocal text
        def repl(match):
            value = make(match)
            if value != match.group(0): changes.append({"written": match.group(0), "spoken": value})
            return value
        text = re.sub(pattern, repl, text, flags=re.I)
    replace(r"\$(\d{1,4})(?![\d.])", lambda m: _number(m.group(1)) + (" dollar" if m.group(1) == "1" else " dollars"))
    replace(r"\b(\d{1,4})%", lambda m: _number(m.group(1)) + " percent")
    units = {"mm": "millimeter", "cm": "centimeter", "km": "kilometer", "kg": "kilogram", "mph": "mile per hour", "fps": "frame per second", "hz": "hertz"}
    def unit(match):
        count = int(match.group(1)); name = units[match.group(2).lower()]
        if count != 1:
            name = {"mile per hour": "miles per hour", "frame per second": "frames per second"}.get(
                name, name if name == "hertz" else name + "s")
        return f"{_number(match.group(1))} {name}"
    replace(r"\b(\d{1,4})\s*(mm|cm|km|kg|mph|fps|hz)\b", unit)
    for pattern, spoken in {r"\bDr\.": "Doctor", r"\bMr\.": "Mister", r"\bMrs\.": "Misses", r"\bvs\.": "versus", r"\be\.g\.": "for example", r"\bi\.e\.": "that is"}.items():
        replace(pattern, lambda _m, value=spoken: value)
    return text, changes

def valid_cmu(value):
    tokens = value.strip().split()
    return bool(tokens) and all(token == "." or re.sub(r"[0-2]$", "", token) in ARPABET for token in tokens)

def prepare_speech_text(script, pronunciations=(), *, phoneme_annotations=True):
    spoken, changes = normalize_spoken(script)
    applied, ignored = [], []
    if phoneme_annotations:
        for entry in sorted(pronunciations or [], key=lambda item: len(str(item.get("text", ""))), reverse=True):
            written = str(entry.get("text", "")).strip(); cmu = str(entry.get("cmu", "")).strip().upper()
            if not written or any(char in written for char in "<>|") or not valid_cmu(cmu):
                ignored.append({"entry": entry, "reason": "invalid pronunciation annotation"}); continue
            pattern = re.compile(r"(?<!\w)" + re.escape(written) + r"(?!\w)")
            spoken, count = pattern.subn(lambda match: f"<{match.group(0)}|{cmu}>", spoken)
            if count: applied.append({"text": written, "cmu": cmu, "occurrences": count})
            else: ignored.append({"entry": entry, "reason": "term not present after normalization"})
    return spoken, {"normalizations": changes, "pronunciations_applied": applied, "pronunciations_ignored": ignored}
