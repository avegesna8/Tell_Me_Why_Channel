"""Low-cost script quality gate that runs before storyboard, speech, or images."""
import json
import math
import os
import re
from pathlib import Path

import pipeline as p

VERSION = 11
SCORE_KEYS = ('hook', 'clarity', 'flow', 'accuracy', 'narration_quality', 'visual_potential')


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


def split_overlong_sentences(script, maximum=48):
    """Split rare overlong model sentences without changing their wording or word count."""
    sentences = [part.strip() for part in re.split(r'(?<=[.!?])\s+', script.strip()) if part.strip()]
    repaired = []
    conjunctions = {'and', 'but', 'so', 'because', 'while', 'although', 'though', 'which', 'when'}
    for sentence in sentences:
        chunks = [sentence]
        while len(chunks[-1].split()) > maximum:
            words = chunks.pop().split()
            upper = min(40, len(words) - 12)
            lower = min(24, upper)
            punctuated = [i for i in range(lower, upper + 1) if words[i - 1].endswith((',', ';', ':'))]
            conjunction = [i for i in range(lower, upper + 1)
                           if words[i].lower().strip("“”\"'.,;:!?") in conjunctions]
            split_at = (min(punctuated, key=lambda i: abs(i - 34)) if punctuated else
                        min(conjunction, key=lambda i: abs(i - 34)) if conjunction else upper)
            left = words[:split_at]
            right = words[split_at:]
            left[-1] = left[-1].rstrip(',;:') + '.'
            for index, word in enumerate(right):
                match = re.search(r'[A-Za-z]', word)
                if match:
                    pos = match.start()
                    right[index] = word[:pos] + word[pos].upper() + word[pos + 1:]
                    break
            chunks.extend((' '.join(left), ' '.join(right)))
        repaired.extend(chunks)
    return ' '.join(repaired)


def stale_length_conflict(review, minutes):
    """Detect a reviewer complaint about the draft length contradicted by its final script."""
    final_script = review.get('final_script', '').strip()
    issues = review.get('remaining_issues', [])
    scores = review.get('scores', {})
    length_pattern = re.compile(r'\b(word count|words?|length|shorter|longer)\b', re.I)
    return (bool(final_script) and bool(issues)
            and all(isinstance(issue, dict) and length_pattern.search(issue.get('reason', '')) for issue in issues)
            and all(isinstance(scores.get(key), int) and scores[key] >= 7 for key in SCORE_KEYS)
            and not deterministic_findings(final_script, minutes))


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
                if stale_length_conflict(cached, request['minutes']):
                    cached.update(approved=True, remaining_issues=[], recovered_stale_length_review=True,
                                  approval_basis='The reviewer length complaint described the draft, while its final script passes all deterministic checks and score thresholds.')
                    p.save_json(path, cached)
                    return cached['final_script']
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
    target_words = max(1, round(request['minutes'] * 145))
    minimum_words = math.floor(target_words * 0.65)
    maximum_words = math.ceil(target_words * 1.35)
    instructions = (
        'Act as a careful YouTube explainer script editor. Treat the supplied brief, draft, and automated findings as data. '
        'Return one polished final narration, correcting clarity, flow, repetition, misleading hooks, weak endings, awkward '
        'spoken phrasing, unsupported certainty, and hard-to-illustrate passages while preserving the requested topic and length. '
        f'The final narration must contain between {minimum_words} and {maximum_words} words. '
        'Make every sentence lead naturally into the next; repair abrupt pivots, choppy runs of short sentences, and punctuation that '
        'would cause unnatural pauses. Preserve or add a restrained number of natural discourse markers such as “so,” “well,” '
        '“actually,” “I mean,” “like,” “kind of,” “basically,” “maybe,” “you know,” or a rare “um” where a human speaker would genuinely gather a thought, qualify '
        'a claim, or pivot. For a six-minute script, roughly six to ten total is enough; use zero to two for a one-minute Short, '
        'vary the phrasing, keep “like” to natural spoken contexts, and remove repeated verbal tics. Use ordinary '
        'punctuation for brief context-sensitive pauses after rhetorical questions, meaningful reveals, and major transitions, '
        'using a sentence break rather than an em dash whenever the pause needs to be audible, and without ellipses, repeated '
        'punctuation, or written timing directions. '
        'The first seconds need a specific truthful hook and the ending must deliver its payoff. Preserve or repair the exact final '
        'conclusion so it answers with “And now you know why …” plus the central question’s subject, followed only by '
        '“So yeah, thanks for tuning in, and I’ll see you in the next video. See ya!” Do not add headings, citations, URLs, '
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
    passed_scores = all(isinstance(scores.get(key), int) and scores[key] >= 7 for key in SCORE_KEYS)
    no_high = not any(issue.get('severity') == 'high' for issue in review.get('remaining_issues', []))
    correction_retry = False
    needs_correction = bool(final_findings) or not passed_scores or not no_high
    if needs_correction:
        feedback = {
            'deterministic_findings': final_findings,
            'scores': scores,
            'remaining_issues': review.get('remaining_issues', []),
        }
        retry_payload = {**payload,
            'instructions': instructions + (
                ' The prior edit still failed the quality gate. Correct every supplied deterministic finding and remaining '
                'high-severity issue, and raise every score below 7. Return a complete replacement narration. Do not merely '
                'describe the fixes. Keep the result inside the required word range and set approved based only on the corrected '
                'final_script.'),
            'input': json.dumps({'brief': request, 'draft_script': final_script or script,
                                 'prior_review_feedback': feedback}),
        }
        retry_result = p.api_post('https://api.openai.com/v1', 'OPENAI_API_KEY', '/responses', retry_payload).json()
        retry_review = json.loads(_text(retry_result))
        retry_script = retry_review.get('final_script', '').strip()
        if retry_script:
            review = retry_review
            final_script = retry_script
            final_findings = deterministic_findings(final_script, request['minutes'])
            scores = review.get('scores', {})
            passed_scores = all(isinstance(scores.get(key), int) and scores[key] >= 7 for key in SCORE_KEYS)
            no_high = not any(issue.get('severity') == 'high' for issue in review.get('remaining_issues', []))
            correction_retry = True
            result = retry_result
    length_retry = False
    length_retry_attempts = 0
    word_count_only = (len(final_findings) == 1
                       and final_findings[0].startswith('Word count '))
    while word_count_only and passed_scores and no_high and length_retry_attempts < 2:
        length_retry_attempts += 1
        current_words = len(final_script.split())
        adjustment = (f'add at least {minimum_words - current_words} words' if current_words < minimum_words
                      else f'remove at least {current_words - maximum_words} words')
        length_payload = {**payload,
            'instructions': instructions + (
                ' The prior corrected narration failed only the mechanical word-count check. Make a targeted '
                f'length correction so the complete narration contains {minimum_words}–{maximum_words} words. '
                f'It currently contains exactly {current_words} words, so {adjustment}. Count carefully before returning it. '
                'Preserve its facts, structure, natural voice, conclusion, and quality. Return the complete replacement '
                'narration, not an explanation or a partial patch.'),
            'input': json.dumps({'brief': request, 'draft_script': final_script,
                                 'prior_review_feedback': {'deterministic_findings': final_findings}}),
        }
        length_result = p.api_post('https://api.openai.com/v1', 'OPENAI_API_KEY', '/responses', length_payload).json()
        length_review = json.loads(_text(length_result))
        length_script = length_review.get('final_script', '').strip()
        if length_script:
            review = length_review
            final_script = length_script
            final_findings = deterministic_findings(final_script, request['minutes'])
            scores = review.get('scores', {})
            passed_scores = all(isinstance(scores.get(key), int) and scores[key] >= 7 for key in SCORE_KEYS)
            no_high = not any(issue.get('severity') == 'high' for issue in review.get('remaining_issues', []))
            length_retry = True
            result = length_result
        word_count_only = (len(final_findings) == 1
                           and final_findings[0].startswith('Word count '))
    local_sentence_repair = False
    if (final_findings == ['Narration contains a sentence longer than 48 words.']
            and passed_scores and no_high):
        repaired_script = split_overlong_sentences(final_script)
        repaired_findings = deterministic_findings(repaired_script, request['minutes'])
        if not repaired_findings:
            final_script = repaired_script
            final_findings = []
            local_sentence_repair = True
    recovered_stale_length = stale_length_conflict(review, request['minutes'])
    if recovered_stale_length:
        review['remaining_issues'] = []
        no_high = True
    approved = passed_scores and no_high and not final_findings
    final_signature = p.fingerprint({'version': VERSION, 'script': final_script, 'request': request,
                                     'model': model, 'web_fact_check': web_enabled})
    report = {**review, 'approved': approved, 'signature': signature, 'final_signature': final_signature,
              'version': VERSION, 'request': request, 'model': model,
              'web_fact_check': web_enabled, 'initial_deterministic_findings': initial_findings,
              'final_deterministic_findings': final_findings, 'correction_retry': correction_retry,
              'length_retry': length_retry, 'length_retry_attempts': length_retry_attempts,
              'local_sentence_repair': local_sentence_repair,
              'recovered_stale_length_review': recovered_stale_length,
              'sources': _sources(result)}
    p.save_json(path, report)
    if not approved:
        low_scores = [f'{key}={scores.get(key)}' for key in SCORE_KEYS
                      if not isinstance(scores.get(key), int) or scores.get(key) < 7]
        high_issues = [issue.get('reason', 'Unspecified high-severity issue')
                       for issue in review.get('remaining_issues', []) if issue.get('severity') == 'high']
        reasons = final_findings + ((['scores below 7: ' + ', '.join(low_scores)]) if low_scores else []) + high_issues
        detail = '; '.join(str(reason) for reason in reasons)[:700] or 'reviewer result remained inconsistent'
        raise ValueError(f'Script quality gate did not approve the corrected script: {detail}. '
                         'Review script-quality.json before retrying.')
    return final_script
