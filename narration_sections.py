"""Split long narration at natural scene boundaries and join verified WAV sections."""
import math
from pathlib import Path


def split_sections(scenes, *, threshold=225, target=145, minimum=100, maximum=180):
    texts = [scene['narration'].strip() for scene in scenes]
    total = sum(len(text.split()) for text in texts)
    if total <= threshold:
        return [' '.join(texts)]
    groups, current, count = [], [], 0
    for text in texts:
        words = len(text.split())
        if current and count >= minimum and count + words > target and current[-1].rstrip().endswith(('.', '?', '!', ';', ':')):
            groups.append(current); current, count = [], 0
        if current and count + words > maximum:
            groups.append(current); current, count = [], 0
        current.append(text); count += words
    if current:
        groups.append(current)
    # Avoid a tiny closing call by moving complete scene slots from the previous section.
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
