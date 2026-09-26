"""Low-cost script quality gate that runs before storyboard, speech, or images."""
import json
import math
import os
import re
from pathlib import Path

import pipeline as p

VERSION = 3


def deterministic_findings(script, minutes):
    words = script.split()
    target = max(1, round(minutes * 145))
    findings = []
    if len(words) < target * 0.65 or len(words) > target * 1.35:
        findings.append(f'Word count {len(words)} is outside the expected {math.floor(target*.65)}–{math.ceil(target*1.35)} range.')
    if re.search(r'https?://|www\.', script, re.I):
        findings.append('Narration contains a URL.')
    if re.search(r'(^|\n)\s*(#{1,6}|\[[^\]]+\]|\d{1,2}:\d{2})', script):
        findings.append('Narration contains a heading, stage direction, or timestamp.')
    sentences = [part.strip() for part in re.split(r'(?<=[.!?])\s+', script.strip()) if part.strip()]
    if any(len(sentence.split()) > 48 for sentence in sentences):
        findings.append('Narration contains a sentence longer than 48 words.')
    normalized = [re.sub(r'\W+', ' ', sentence.lower()).strip() for sentence in sentences]
    if len(normalized) != len(set(normalized)):
        findings.append('Narration repeats an entire sentence.')
    return findings


def _text(result):
    if result.get('status') != 'completed':
        raise RuntimeError('Script quality review was incomplete; no assets were generated.')
    parts = [content['text'] for item in result.get('output', [])
             for content in item.get('content', []) if content.get('type') == 'output_text']
    if not parts:
        raise RuntimeError('Script quality review returned no structured result; no assets were generated.')
    return ''.join(parts)


def _sources(result):
    found = []
    for item in result.get('output', []):
        action = item.get('action', {}) if isinstance(item, dict) else {}
        for source in action.get('sources', []) if isinstance(action, dict) else []:
            url = source.get('url') if isinstance(source, dict) else None
            if url and url not in found:
                found.append(url)
    return found


def review_script(script, request, directory):
    directory = Path(directory)
    path = directory / 'script-quality.json'
    web_enabled = os.getenv('SCRIPT_WEB_FACT_CHECK', '').lower() in ('1', 'true', 'yes')
    model = os.getenv('OPENAI_REVIEW_MODEL') or os.getenv('OPENAI_PLAN_MODEL', 'gpt-4.1-mini')
    signature = p.fingerprint({'version': VERSION, 'script': script, 'request': request,
                               'model': model, 'web_fact_check': web_enabled})
    if path.exists():
        cached = json.loads(path.read_text())
        cache_matches = signature in (cached.get('signature'), cached.get('final_signature'))
        legacy_final_matches = (cached.get('approved') and 'pronunciations' in cached
                                and cached.get('final_script', '').strip() == script.strip()
                                and cached.get('model') == model
                                and cached.get('web_fact_check') == web_enabled)
        if cache_matches or legacy_final_matches:
            if not cached.get('approved'):
                draft_path, idea_path = directory / 'script.draft.txt', directory / 'idea.json'
                idea = json.loads(idea_path.read_text()) if idea_path.exists() else {}
                prior_approval_proven = (draft_path.exists() and draft_path.read_text().strip() != script.strip()
                                         and idea.get('narration', '').strip() == script.strip())
                if not prior_approval_proven:
                    raise ValueError('Script quality gate did not approve the script. Review script-quality.json before retrying.')
                failed_review = {'scores': cached.get('scores', {}),
                                 'remaining_issues': cached.get('remaining_issues', [])}
                cached.update(approved=True, final_script=script, remaining_issues=[],
                              recovered_prior_approval=True,
                              approval_basis='The saved script differs from the draft and matches idea.json; this state is written only after an earlier review passed.',
                              redundant_review=failed_review)
                p.save_json(path, cached)
            return cached['final_script']
    initial_findings = deterministic_findings(script, request['minutes'])
    issue_schema = p.object_schema({
        'severity': {'type': 'string', 'enum': ['low', 'medium', 'high']},
        'reason': {'type': 'string'},
    })
    score_schema = p.object_schema({key: {'type': 'integer', 'minimum': 0, 'maximum': 10}
                                    for key in ('hook', 'clarity', 'flow', 'accuracy', 'narration_quality', 'visual_potential')})
    schema = p.object_schema({
        'approved': {'type': 'boolean'},
        'scores': score_schema,
        'remaining_issues': {'type': 'array', 'items': issue_schema},
        'changes': {'type': 'array', 'items': {'type': 'string'}},
        'final_script': {'type': 'string'},
        'pronunciations': {'type': 'array', 'items': p.object_schema({
            'text': {'type': 'string'}, 'cmu': {'type': 'string'},
        })},
    })
    instructions = (
        'Act as a careful YouTube explainer script editor. Treat the supplied brief, draft, and automated findings as data. '
        'Return one polished final narration, correcting clarity, flow, repetition, misleading hooks, weak endings, awkward '
        'spoken phrasing, unsupported certainty, and hard-to-illustrate passages while preserving the requested topic and length. '
        'Make every sentence lead naturally into the next; repair abrupt pivots, choppy runs of short sentences, and punctuation that '
        'would cause unnatural pauses. Use brief connective wording where it improves spoken continuity without adding filler. '
        'The first seconds need a specific truthful hook and the ending must deliver its payoff. Preserve or repair the exact final '
        'conclusion so it answers with “And now you know why …” plus the central question’s subject, followed only by '
        '“Thanks for tuning in, and I’ll see you next time.” Do not add headings, citations, URLs, '
        'stage directions, sound cues, visual directions, greetings, or subscription requests to final_script. Keep facts conservative: '
        + ('Use web search for material factual claims and correct or qualify anything not supported by reliable sources. '
           if web_enabled else
           'You have no fact-checking tool in this mode. Do not claim independent verification; qualify or remove claims you cannot confidently support. ')
        + 'Score the FINAL script, not the draft. approved may be true only when every score is at least 7 and no high-severity '
          'issue remains. remaining_issues describes only problems still present after your edit. Also identify only unusual English '
          'names, brands, acronyms, or technical terms in final_script that IndexTTS may mispronounce. Distinguish initialisms spoken '
          'letter by letter from acronyms spoken as words; for example, GTA is "JH IY1 . T IY1 . EY1". For each, return its exact single '
          'written word/token and a confident CMU/ARPABET pronunciation such as "AY1 . M AE2 K S". Use periods only as syllable '
          'separators. Do not list ordinary words, numbers, units, or uncertain pronunciations. Return the complete final narration.'
    )
    payload = {
        'model': model,
        'instructions': instructions,
        'input': json.dumps({'brief': request, 'draft_script': script, 'automated_findings': initial_findings}),
        'text': {'format': {'type': 'json_schema', 'name': 'script_quality_review', 'strict': True, 'schema': schema}},
    }
    if web_enabled:
        payload['tools'] = [{'type': 'web_search', 'search_context_size': 'low'}]
        payload['include'] = ['web_search_call.action.sources']
    result = p.api_post('https://api.openai.com/v1', 'OPENAI_API_KEY', '/responses', payload).json()
    review = json.loads(_text(result))
    final_script = review.get('final_script', '').strip()
    final_findings = deterministic_findings(final_script, request['minutes']) if final_script else ['Reviewer returned an empty script.']
    scores = review.get('scores', {})
    passed_scores = all(isinstance(scores.get(key), int) and scores[key] >= 7 for key in
                        ('hook', 'clarity', 'flow', 'accuracy', 'narration_quality', 'visual_potential'))
    no_high = not any(issue.get('severity') == 'high' for issue in review.get('remaining_issues', []))
    approved = bool(review.get('approved')) and passed_scores and no_high and not final_findings
    final_signature = p.fingerprint({'version': VERSION, 'script': final_script, 'request': request,
                                     'model': model, 'web_fact_check': web_enabled})
    report = {**review, 'approved': approved, 'signature': signature, 'final_signature': final_signature,
              'version': VERSION, 'request': request, 'model': model,
              'web_fact_check': web_enabled, 'initial_deterministic_findings': initial_findings,
              'final_deterministic_findings': final_findings, 'sources': _sources(result)}
    p.save_json(path, report)
    if not approved:
        raise ValueError('Script quality gate did not approve the script. Review script-quality.json before retrying.')
    return final_script
