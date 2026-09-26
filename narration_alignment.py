"""Match storyboard words to a continuous narration's speech timestamps."""
from difflib import SequenceMatcher
import math
import re


_WHISPER_MODELS = {}


def normalized(word):
    return re.sub(r"[^\w]", "", word.casefold())


def align_words(scenes, recognized, audio_duration):
    script = []
    ranges = []
    for scene in scenes:
        start = len(script)
        script.extend(w for w in scene["narration"].split() if normalized(w))
        ranges.append((start, len(script)))
    spoken = [w for w in recognized if normalized(w["word"])]
    previous = 0.0
    for word in spoken:
        start, end = word["start"], word["end"]
        if not all(math.isfinite(t) for t in (start, end)) or not 0 <= start <= end <= audio_duration + 0.1 or start < previous:
            raise ValueError("Speech recognition returned invalid timestamps.")
        previous = start
    matcher = SequenceMatcher(None, [normalized(w) for w in script],
                              [normalized(w["word"]) for w in spoken], autojunk=False)
    matches = {}
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            matches[a + offset] = spoken[b + offset]
    if not script or len(matches) / len(script) < 0.8:
        raise ValueError("Narration does not closely match the storyboard. Review narration and transcript before rendering.")
    if any(end == start for start, end in ranges):
        raise ValueError("Every scene needs spoken narration words.")
    scene_matches = [sum(i in matches for i in range(start, end)) / (end-start)
                     for start, end in ranges]
    aligned = [None] * len(script)
    for i, word in matches.items():
        aligned[i] = {"word": script[i], "start": word["start"], "end": min(word["end"], audio_duration)}
    # Interpolate small recognition mismatches between reliable speech anchors.
    i = 0
    while i < len(aligned):
        if aligned[i] is not None:
            i += 1
            continue
        end = i
        while end < len(aligned) and aligned[end] is None:
            end += 1
        left = aligned[i-1]["end"] if i else 0.0
        right = aligned[end]["start"] if end < len(aligned) else audio_duration
        right = max(left, right)
        for j in range(i, end):
            aligned[j] = {"word": script[j], "start": left+(right-left)*(j-i)/(end-i),
                          "end": left+(right-left)*(j-i+1)/(end-i)}
        i = end
    boundaries = [0.0]
    for start, _ in ranges[1:]:
        # Change the image at the first word of the new scene, retaining pauses.
        boundaries.append(aligned[start]["start"])
    boundaries.append(audio_duration)
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError("Scene boundaries are not increasing; review the narration alignment.")
    return {"boundaries": boundaries, "words": aligned,
            "matched_fraction": len(matches) / len(script),
            "scene_matched_fractions": scene_matches}


def transcribe(audio_path, model_name="small"):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Install faster-whisper to align continuous narration: pip install faster-whisper") from exc
    key = (model_name, "cpu", "int8")
    model = _WHISPER_MODELS.get(key)
    if model is None:
        print(f"Loading Whisper ({model_name}); first use may download the model…", flush=True)
        model = WhisperModel(model_name, device=key[1], compute_type=key[2])
        _WHISPER_MODELS[key] = model
    print(f"Aligning narration locally with Whisper ({model_name})…", flush=True)
    segments, _ = model.transcribe(str(audio_path), word_timestamps=True, vad_filter=False)
    words = []
    for segment in segments:
        words.extend({"word": w.word.strip(), "start": w.start, "end": w.end} for w in (segment.words or []))
        print(f"  Transcribed through {segment.end:.1f}s", flush=True)
    return words
