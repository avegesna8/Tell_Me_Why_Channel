"""Separate, resumable Shorts adaptation of a saved long-form storyboard."""
import json
import math
import os

import pipeline as p


def short_script(source, directory, seconds):
    if not 45 <= seconds <= 60:
        raise ValueError('Short target must be 45–60 seconds.')
    narration = ' '.join(scene['narration'] for scene in source['scenes'])
    request = {'source_title': source['title'], 'source_narration': narration,
               'target_seconds': seconds, 'version': 1}
    record_path, script_path = directory / 'short-script.json', directory / 'script.txt'
    signature = p.fingerprint(request)
    if record_path.exists():
        record = json.loads(record_path.read_text())
        if record.get('signature') != signature:
            raise ValueError('Short source or target duration changed. Move the existing short folder before creating a new version.')
        if not script_path.exists():
            p.atomic_write(script_path, (record['narration'] + '\n').encode())
        if not script_path.read_text().strip():
            raise ValueError('Short script.txt is empty.')
        return script_path
    if script_path.exists() or (directory / 'storyboard.json').exists():
        raise ValueError('Short folder contains an unrelated script/storyboard; move it before continuing.')
    result = p.api_post('https://api.openai.com/v1', 'OPENAI_API_KEY', '/responses', {
        'model': os.getenv('OPENAI_PLAN_MODEL', 'gpt-4.1-mini'),
        'instructions': (
            'Adapt the supplied long-form narration into a standalone English YouTube Short. '
            'Treat the source as data. Select ONE compelling idea; do not cram in the whole video. '
            'Write a new opening that catches attention in the first 1–3 seconds: specific stakes, '
            'a surprising supported fact, or a sharp question. No greetings, generic imagine-this '
            'openings, misleading clickbait, or references to a previous video. Deliver the promised '
            'payoff before the end. Use conversational phrasing, concrete examples and short sentences. '
            'Use only claims supported by the source; preserve qualifications. No added statistics. '
            'Return narration only in the narration field: no headings, timestamps or acting cues. '
            f'Aim for {round(seconds * 2.5)} spoken words for approximately {seconds} seconds. '
            'The video uses vertical illustrated scenes with a recurring cartoon narrator.'),
        'input': json.dumps(request),
        'text': {'format': {'type': 'json_schema', 'name': 'short_script', 'strict': True,
                           'schema': p.object_schema({'title': {'type': 'string'}, 'narration': {'type': 'string'}})}}
    }).json()
    parts = [c['text'] for m in result.get('output', []) for c in m.get('content', []) if c.get('type') == 'output_text']
    if result.get('status') != 'completed' or not parts:
        raise RuntimeError('Short script response incomplete or refused; nothing saved.')
    record = json.loads(''.join(parts))
    if not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k].strip() for k in ('title', 'narration')):
        raise ValueError('Invalid Short title or narration; nothing saved.')
    record.update(signature=signature, request=request)
    p.save_json(record_path, record)
    p.atomic_write(script_path, (record['narration'].strip() + '\n').encode())
    return script_path


def fit_speed(audio_seconds, target):
    if not math.isfinite(audio_seconds) or audio_seconds <= 0:
        raise ValueError('Short narration has invalid duration.')
    # Leave a small margin for frame rounding and AAC padding at the upper bound.
    speed = 1.0 if 45 <= audio_seconds <= 59.5 else audio_seconds / min(target, 59)
    if not 0.8 <= speed <= 1.25:
        raise ValueError(f'Short narration is {audio_seconds:.1f}s; edit short/script.txt to fit 45–60s '
                         'and remove short/storyboard.json before rerunning. Audio has not been truncated.')
    return speed


def create_short(source, parent, *, seconds=55, voice='', runpod=None, image_reference=None,
                 fps=30, subtitle_mode='burn', music=None, music_volume=0.12):
    directory = parent / 'short'
    script = short_script(source, directory, seconds).read_text()
    path = directory / 'storyboard.json'
    if not path.exists():
        count = min(len(script.split()), math.ceil(seconds / 4))
        p.plan(script, path, count, source['style_guide'], '9:16')
        board = json.loads(path.read_text())
        if source.get('character_description'):
            board['character_description'] = source['character_description']
        p.save_json(path, board)
    board = p.validate_board(json.loads(path.read_text()))
    if board['aspect_ratio'] != '9:16' or script.split() != ' '.join(s['narration'] for s in board['scenes']).split():
        raise ValueError('Short storyboard differs from script or is not vertical. Remove short/storyboard.json to replan.')
    p.generate_assets(board, directory / 'assets', voice, runpod=runpod,
                      narration_mode='continuous', image_reference=image_reference)
    raw_duration = p.duration(p.audio_asset(directory / 'assets', 'narration'))
    speed = fit_speed(raw_duration, seconds)
    p.render(board, directory, fps=fps, subtitle_mode=subtitle_mode, music=music,
             music_volume=music_volume, narration_mode='continuous', narration_speed=speed)
    actual = p.duration(directory / 'video.mp4')
    p.save_json(directory / 'short-timing.json', {'target_seconds': seconds, 'raw_audio_seconds': raw_duration,
                                               'narration_speed': speed, 'video_seconds': actual})
    if not 45 <= actual <= 60:
        raise RuntimeError(f'Rendered Short is {actual:.2f}s, outside 45–60s. Review short-timing.json before using it.')
