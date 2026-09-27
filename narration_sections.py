"""Split narration at natural sentence boundaries and join verified WAV sections."""
import math
from pathlib import Path
import re


def _sentences(text):
    # Protect initials and dotted initialisms ("Warren G. Harding", "U.S.")
    # before splitting on terminal punctuation.
    marker = "<PERIOD>"
    protected = re.sub(r"\b([A-Z])\.", rf"\1{marker}", text)
    parts = re.split(r"(?<=[.!?])\s+", protected.strip())
    return [part.replace(marker, ".").strip() for part in parts if part.strip()]


def split_sections(scenes, *, threshold=100, target=65, minimum=40, maximum=85):
    text = ' '.join(scene['narration'].strip() for scene in scenes)
    total = len(text.split())
    if total <= threshold:
        return [text]
    sentences = _sentences(text)
    groups, current, count = [], [], 0
    for sentence in sentences:
        words = len(sentence.split())
        if current and count >= minimum and count + words > target:
            groups.append(current); current, count = [], 0
        if current and count + words > maximum:
            groups.append(current); current, count = [], 0
        current.append(sentence); count += words
    if current:
        groups.append(current)
    # Avoid a tiny closing call by moving one complete sentence from the prior section.
    while len(groups) > 1 and sum(len(x.split()) for x in groups[-1]) < minimum * .7 and len(groups[-2]) > 1:
        groups[-1].insert(0, groups[-2].pop())
    return [' '.join(group) for group in groups]


def join_wavs(parts, output, run_ffmpeg, *, pause_seconds=.18):
    if len(parts) < 2:
        raise ValueError('Section joining requires at least two audio files.')
    if not math.isfinite(pause_seconds) or not 0 <= pause_seconds <= .5:
        raise ValueError('Narration section pause must be between 0 and 0.5 seconds.')
    args, filters, labels = [], [], []
    for index, part in enumerate(parts):
        args += ['-i', str(Path(part))]
        label = f's{index}'
        chain = (f'[{index}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono,'
                 'silenceremove=start_periods=1:start_duration=0.015:start_threshold=-45dB,'
                 'areverse,silenceremove=start_periods=1:start_duration=0.04:start_threshold=-45dB,areverse,'
                 'loudnorm=I=-18:TP=-2:LRA=7')
        if index < len(parts) - 1 and pause_seconds:
            chain += f',apad=pad_dur={pause_seconds}'
        filters.append(chain + f'[{label}]'); labels.append(f'[{label}]')
    filters.append(''.join(labels) + f'concat=n={len(parts)}:v=0:a=1[joined]')
    run_ffmpeg(args + ['-filter_complex', ';'.join(filters), '-map', '[joined]', '-ar', '48000', '-ac', '1', str(output)])
