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
    for size, label in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
        if n >= size:
            return _number(str(n // size)) + " " + label + (" " + _number(str(n % size)) if n % size else "")
    return value

def _quantity(value):
    clean = value.replace(",", "")
    if "." not in clean or set(clean.split(".", 1)[1]) == {"0"}:
        return _number(clean.split(".", 1)[0])
    whole, fraction = clean.split(".", 1)
    digits = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    return _number(whole) + " point " + " ".join(digits[int(digit)] for digit in fraction)

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
    def currency(match):
        amount, magnitude = match.group(1), (match.group(2) or "").lower()
        singular = not magnitude and float(amount.replace(",", "")) == 1
        return _quantity(amount) + (" " + magnitude if magnitude else "") + (" dollar" if singular else " dollars")
    replace(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*(thousand|million|billion|trillion)?\b", currency)
    replace(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)%", lambda m: _quantity(m.group(1)) + " percent")
    # IndexTTS does not reliably pause at an em dash. Use a speech-only sentence break;
    # the written script and captions retain their original punctuation.
    replace(r"\s*—\s*([A-Za-z])", lambda m: ". " + m.group(1).upper())
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

def prepare_speech_text(script, pronunciations=(), *, phoneme_annotations=True,
                        apply_suggested_pronunciations=False):
    spoken, changes = normalize_spoken(script)
    applied, ignored = [], []
    if phoneme_annotations:
        automatic = [
            {"text": "GTA", "cmu": "JH IY1 . T IY1 . EY1"},
            {"text": "pricier", "cmu": "P R AY1 S IY0 ER0"},
        ]
        automatic = [entry for entry in automatic
                     if re.search(r"(?<!\w)" + re.escape(entry["text"]) + r"(?!\w)", spoken)]
        suggestions = list(pronunciations or [])
        if not apply_suggested_pronunciations:
            ignored.extend({"entry": entry, "reason": "AI-suggested pronunciation retained for review but not sent to TTS"}
                           for entry in suggestions)
            suggestions = []
        entries = suggestions + automatic
        seen = set()
        for entry in sorted(entries, key=lambda item: len(str(item.get("text", ""))), reverse=True):
            written = str(entry.get("text", "")).strip(); cmu = str(entry.get("cmu", "")).strip().upper()
            if written.casefold() in seen:
                continue
            seen.add(written.casefold())
            if not written or any(char in written for char in "<>|") or not valid_cmu(cmu):
                ignored.append({"entry": entry, "reason": "invalid pronunciation annotation"}); continue
            pattern = re.compile(r"(?<!\w)" + re.escape(written) + r"(?!\w)")
            spoken, count = pattern.subn(lambda match: f"<{match.group(0)}|{cmu}>", spoken)
            if count: applied.append({"text": written, "cmu": cmu, "occurrences": count})
            else: ignored.append({"entry": entry, "reason": "term not present after normalization"})
    return spoken, {"normalizations": changes, "pronunciations_applied": applied, "pronunciations_ignored": ignored}
