import os, re, subprocess, sys, shutil
from datetime import timedelta

# ---------- ffmpeg binary (prefer venv one) ----------
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    FFPROBE = getattr(imageio_ffmpeg, "get_ffprobe_exe", None)
except Exception:
    FFMPEG = shutil.which("ffmpeg")
    FFPROBE = shutil.which("ffprobe")

if not FFMPEG:
    sys.exit("No ffmpeg found. `pip install imageio-ffmpeg` or install ffmpeg system-wide.")

# ---------- inputs ----------
AUDIO = "audio.mp3"           # your generated audio
CHAR  = "character.png"       # PNG with alpha
BG    = "background.mp4"      # set to None for solid color
TEXT  = """Today's news football style will be about how bad Russell Wilson is lol.
And also a quick update on the playoff picture!"""

OUT_VIDEO        = "out.mp4"
OUT_VIDEO_BURNED = "out_burned.mp4"
OUT_VIDEO_SOFT   = "out_softsubs.mp4"
OUT_SRT          = "subs.srt"

# frame & layout
WIDTH, HEIGHT, FPS = 1080, 1920, 30
CHAR_WIDTH_RATIO   = 0.45
BOTTOM_MARGIN      = 220
BOB_PIXELS         = 8
BOB_HZ             = 0.5

# subtitle config
SUB_MODE            = "phrase"  # "phrase" or "word"
TARGET_WORDS_PER_LINE = 6
MIN_PHRASE_WORDS    = 3
MAX_PHRASE_WORDS    = 12
BURN_SUBS           = True      # True = burn into pixels, False = soft subs
USE_EXISTING_SRT    = True      # True = use pre-generated subs.srt if available


# ---------- helpers ----------
def ffprobe_duration_seconds(path: str) -> float:
    if FFPROBE:
        cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        out = subprocess.run(cmd, capture_output=True, text=True)
        return float(out.stdout.strip())
    else:
        cmd = [FFMPEG, "-i", path, "-f", "null", "-"]
        out = subprocess.run(cmd, capture_output=True, text=True)
        txt = out.stderr
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", txt)
        if not m:
            raise RuntimeError(f"Couldn't parse duration from: {txt[:200]}…")
        h, m_, s = m.groups()
        return int(h) * 3600 + int(m_) * 60 + float(s)


def srt_timestamp(t: float) -> str:
    td = timedelta(seconds=max(0, t))
    total_seconds = int(td.total_seconds())
    ms = int((t - total_seconds) * 1000)
    hh = total_seconds // 3600
    mm = (total_seconds % 3600) // 60
    ss = total_seconds % 60
    return f"{hh:02}:{mm:02}:{ss:02},{ms:03}"


def split_into_phrases(text: str) -> list[list[str]]:
    text = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"([.!?;:])", text)
    sentences = []
    for i in range(0, len(parts), 2):
        chunk = parts[i].strip()
        punct = parts[i+1] if i+1 < len(parts) else ""
        if chunk:
            sentences.append((chunk + (punct or "")).strip())

    phrases = []
    for sent in sentences:
        words = sent.split()
        i = 0
        while i < len(words):
            take = min(MAX_PHRASE_WORDS, max(MIN_PHRASE_WORDS, TARGET_WORDS_PER_LINE))
            phrases.append(words[i:i+take])
            i += take
    return phrases


def write_srt_from_text(text: str, audio_len: float, path: str, mode: str = "phrase"):
    words = text.strip().split()
    if not words:
        raise ValueError("No words in text for subtitles.")

    total_words = len(words)
    avg_time_per_word = audio_len / total_words
    entries = []

    if mode == "word":
        t = 0.0
        for i, w in enumerate(words, 1):
            start = t
            end = start + avg_time_per_word
            entries.append((i, start, end, w))
            t = end
    else:
        phrases = split_into_phrases(text)
        t = 0.0
        idx = 1
        for chunk in phrases:
            chunk_words = len(chunk)
            dur = chunk_words * avg_time_per_word
            start = t
            end = min(t + dur, audio_len)
            line = " ".join(chunk)
            entries.append((idx, start, end, line))
            idx += 1
            t = end

    with open(path, "w", encoding="utf-8") as f:
        for idx, start, end, line in entries:
            f.write(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{line}\n\n")


# ---------- 1) build the base video ----------
def build_video():
    for p in [AUDIO, CHAR]:
        if not os.path.exists(p):
            sys.exit(f"Missing file: {p}")

    inputs, fc = [], []

    if BG and os.path.exists(BG):
        inputs += ["-stream_loop", "-1", "-i", BG]
        fc.append(
            f"[0:v]scale=w={WIDTH}:h={HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop=w={WIDTH}:h={HEIGHT},setsar=1[vbg]"
        )
    else:
        inputs += ["-f", "lavfi", "-i", f"color=c=skyblue:s={WIDTH}x{HEIGHT}:r={FPS}"]
        fc.append("[0:v]format=yuva444p,setsar=1[vbg]")

    inputs += ["-i", AUDIO, "-loop", "1", "-i", CHAR]

    char_px = int(WIDTH * CHAR_WIDTH_RATIO)
    fc.append(
        f"[2:v]scale={char_px}:-1,format=rgba,"
        f"colorchannelmixer=aa=0.96,"
        f"fade=t=in:st=0:d=0.25:alpha=1[char]"
    )
    fc.append(
        "[vbg][char]overlay="
        f"x=(W-w)/2:y=H-h-{BOTTOM_MARGIN}+{BOB_PIXELS}*sin(2*PI*t*{BOB_HZ}):"
        "shortest=1,format=yuv420p[v]"
    )

    cmd = (
        [FFMPEG, "-nostdin", "-y"] +
        inputs + [
            "-filter_complex", "; ".join(fc),
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-c:a", "aac",
            "-shortest",
            OUT_VIDEO
        ]
    )
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print("Wrote", OUT_VIDEO)


# ---------- 2) add subtitles ----------
def add_subtitles(text: str):
    if USE_EXISTING_SRT and os.path.exists(OUT_SRT):
        srt_path = OUT_SRT
        print(f"Using existing subtitles: {srt_path}")
    else:
        dur = ffprobe_duration_seconds(AUDIO)
        write_srt_from_text(text, dur, OUT_SRT, mode=SUB_MODE)
        srt_path = OUT_SRT
        print(f"Generated subtitles: {srt_path}")

    if BURN_SUBS:
        cmd = [
            FFMPEG, "-nostdin", "-y",
            "-i", OUT_VIDEO,
            "-vf", f"subtitles={srt_path}:force_style='Fontsize=36,Outline=2,BorderStyle=1,Alignment=2'",
            "-c:a", "copy",
            OUT_VIDEO_BURNED
        ]
        print("Burning subs…")
        subprocess.run(cmd, check=True)
        print("Wrote", OUT_VIDEO_BURNED)
    else:
        cmd = [
            FFMPEG, "-nostdin", "-y",
            "-i", OUT_VIDEO, "-i", srt_path,
            "-c", "copy", "-c:s", "mov_text",
            OUT_VIDEO_SOFT
        ]
        print("Muxing soft subs…")
        subprocess.run(cmd, check=True)
        print("Wrote", OUT_VIDEO_SOFT)


if __name__ == "__main__":
    build_video()
    add_subtitles(TEXT)