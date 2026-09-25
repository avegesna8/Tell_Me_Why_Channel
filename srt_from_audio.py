import srt, math
from datetime import timedelta
from faster_whisper import WhisperModel

AUDIO = "audio.mp3"             # your TTS output
MODEL_SIZE = "small"            # "base", "small", "medium"; small is a good balance
MAX_LINE_CHARS = 42             # wrap lines nicely
MAX_CUE_SEC = 5.0               # avoid very long subtitles
MIN_CUE_SEC = 0.7               # avoid blink-fast subs
OUT_SRT = "subs.srt"

def group_words(words, max_chars=MAX_LINE_CHARS, max_dur=MAX_CUE_SEC):
    """
    Group word items (each has 'word','start','end') into subtitle chunks
    by character budget and max duration.
    """
    chunk, chunks = [], []
    chunk_start = None
    for w in words:
        if chunk_start is None:
            chunk_start = w["start"]
        new_text = (" ".join([x["word"] for x in chunk] + [w["word"]])).strip()
        new_dur = (w["end"] - chunk_start)
        if len(new_text) > max_chars or new_dur > max_dur:
            if chunk:
                chunks.append((chunk_start, chunk[-1]["end"], " ".join(x["word"] for x in chunk)))
            chunk, chunk_start = [w], w["start"]
        else:
            chunk.append(w)
    if chunk:
        chunks.append((chunk_start, chunk[-1]["end"], " ".join(x["word"] for x in chunk)))
    return chunks

def clamp_duration(start, end, min_sec=MIN_CUE_SEC):
    if end - start < min_sec:
        end = start + min_sec
    return start, end

def to_srt(chunks):
    subs = []
    for i, (st, en, text) in enumerate(chunks, 1):
        st, en = clamp_duration(st, en)
        subs.append(srt.Subtitle(
            index=i,
            start=timedelta(seconds=st),
            end=timedelta(seconds=en),
            content=text
        ))
    return srt.compose(subs)

def main():
    model = WhisperModel(MODEL_SIZE, compute_type="auto")  # CPU/GPU auto
    segments, _ = model.transcribe(AUDIO, word_timestamps=True)
    words = []
    for seg in segments:
        if not seg.words:  # fallback to segment timing
            words.append({"word": seg.text.strip(), "start": seg.start, "end": seg.end})
        else:
            for w in seg.words:
                words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
    # clean empties
    words = [w for w in words if w["word"]]
    chunks = group_words(words)
    with open(OUT_SRT, "w", encoding="utf-8") as f:
        f.write(to_srt(chunks))
    print("Wrote", OUT_SRT)

if __name__ == "__main__":
    main()
