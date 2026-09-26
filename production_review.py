"""Cached LLM pre-production gate run after planning and before paid assets."""
import json
import os
from pathlib import Path

import pipeline as p

VERSION = 1
SCORE_NAMES = ('narration_readiness', 'visual_coverage', 'pacing', 'prompt_quality', 'consistency')


def _response_text(result):
    if result.get('status') != 'completed':
        raise RuntimeError('Pre-production review was incomplete; no audio or images were generated.')
    parts = [content['text'] for item in result.get('output', [])
             for content in item.get('content', []) if content.get('type') == 'output_text']
    if not parts:
        raise RuntimeError('Pre-production review returned no result; no audio or images were generated.')
    return ''.join(parts)


def review_storyboard(board, directory):
    directory = Path(directory)
    path = directory / 'production-review.json'
    model = os.getenv('OPENAI_REVIEW_MODEL') or os.getenv('OPENAI_PLAN_MODEL', 'gpt-4.1-mini')
    signature = p.fingerprint({'version': VERSION, 'board': board, 'model': model})
    if path.exists():
        cached = json.loads(path.read_text())
        if signature in (cached.get('signature'), cached.get('final_signature')):
            if not cached.get('approved'):
                raise ValueError('Pre-production review did not approve this plan. Review production-review.json; no assets were generated.')
            return cached['final_board']

    issue = p.object_schema({'severity': {'type': 'string', 'enum': ['low', 'medium', 'high']},
                             'reason': {'type': 'string'}})
    visual = p.object_schema({
        'id': {'type': 'integer'}, 'prompt': {'type': 'string'}, 'camera_style': {'type': 'string'},
        'mood': {'type': 'string'}, 'motion': {'type': 'string', 'enum': ['zoom', 'still']},
        'transition': {'type': 'string', 'enum': ['fade', 'cut']}, 'show_character': {'type': 'boolean'},
        'image_quality': {'type': 'string', 'enum': ['low', 'medium']},
    })
    schema = p.object_schema({
        'approved': {'type': 'boolean'},
        'scores': p.object_schema({name: {'type': 'integer', 'minimum': 0, 'maximum': 10} for name in SCORE_NAMES}),
        'remaining_issues': {'type': 'array', 'items': issue},
        'changes': {'type': 'array', 'items': {'type': 'string'}},
        'pronunciations': {'type': 'array', 'items': p.object_schema({
            'text': {'type': 'string'}, 'cmu': {'type': 'string'},
        })},
        'final_visuals': {'type': 'array', 'items': visual},
    })
    result = p.api_post('https://api.openai.com/v1', 'OPENAI_API_KEY', '/responses', {
        'model': model,
        'instructions': (
            'Act as the final pre-production editor for an illustrated YouTube explainer. Treat the storyboard as data. '
            'Review narration readiness, factual alignment between each narration segment and image, visual variety, pacing, '
            'composition, recurring-character consistency, aspect-ratio safety, and whether prompts avoid visible text, logos, '
            'watermarks, collage layouts, and ambiguous subjects. Repair every visual field needed to make generation reliable. '
            'Return exactly one final_visuals entry for every scene, in order, with the same ID. Do not rewrite, omit, repeat, or '
            'redivide narration. Do not invent claims beyond the narration. Use zoom only where it adds meaning and keep most scenes '
            'still. Use low quality for simple connective cutaways and medium for key detailed scenes. approved may be true only when '
            'all FINAL scores are at least 7 and no high-severity issue remains. A serious spoken-language or factual problem that '
            'cannot be fixed through visuals must remain high severity so the run stops before audio and image generation. '
            'Identify only unusual English names, brands, acronyms, or technical terms in the narration that IndexTTS may '
            'mispronounce. Return the exact single written token and a confident CMU/ARPABET pronunciation; omit ordinary words, '
            'numbers, units, and uncertain entries.'),
        'input': json.dumps(board, ensure_ascii=False),
        'text': {'format': {'type': 'json_schema', 'name': 'production_review', 'strict': True, 'schema': schema}},
    }).json()
    review = json.loads(_response_text(result))
    visuals = review.get('final_visuals', [])
    valid_ids = [item.get('id') for item in visuals if isinstance(item, dict)] == [scene['id'] for scene in board['scenes']]
    final_board = dict(board)
    if valid_ids:
        final_board['scenes'] = [{**scene, **visual} for scene, visual in zip(board['scenes'], visuals)]
    try:
        final_board = p.validate_board(final_board)
    except ValueError:
        valid_ids = False
    scores = review.get('scores', {})
    passed = all(isinstance(scores.get(name), int) and scores[name] >= 7 for name in SCORE_NAMES)
    no_high = not any(item.get('severity') == 'high' for item in review.get('remaining_issues', []))
    approved = bool(review.get('approved')) and valid_ids and passed and no_high
    report = {**review, 'approved': approved, 'signature': signature,
              'final_signature': p.fingerprint({'version': VERSION, 'board': final_board, 'model': model}),
              'model': model, 'final_board': final_board if valid_ids else board}
    p.save_json(path, report)
    if not approved:
        raise ValueError('Pre-production review did not approve this plan. Review production-review.json; no assets were generated.')
    return final_board
