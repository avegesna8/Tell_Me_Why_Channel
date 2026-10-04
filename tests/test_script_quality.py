import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import script_quality as q


def review_response(*, approved=True, score=8, script=None, severity='low'):
    final = script or ' '.join(f'Point {i} adds a clear useful detail.' for i in range(24))
    body = {'approved': approved,
            'scores': {key: score for key in ('hook','clarity','flow','accuracy','narration_quality','visual_potential')},
            'remaining_issues': [] if approved else [{'severity': severity, 'reason': 'Needs correction.'}],
            'changes': ['Improved spoken flow.'], 'final_script': final, 'pronunciations': []}
    response = Mock()
    response.json.return_value = {'status':'completed','output':[{'type':'message','content':[{
        'type':'output_text','text':json.dumps(body)}]}]}
    return response


class ScriptQualityTests(unittest.TestCase):
    def test_one_review_call_is_cached_and_web_is_off_by_default(self):
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        request = {'idea':'A topic','minutes':1}
        with tempfile.TemporaryDirectory() as tmp, patch.object(q.p, 'api_post', return_value=review_response()) as post, \
             patch.dict('os.environ', {'SCRIPT_WEB_FACT_CHECK':'0','OPENAI_PLAN_MODEL':'gpt-4.1-mini'}, clear=False):
            first = q.review_script(draft, request, Path(tmp))
            second = q.review_script(draft, request, Path(tmp))
            self.assertEqual(first, second)
            self.assertEqual(post.call_count, 1)
            payload = post.call_args.args[3]
            self.assertNotIn('tools', payload)
            self.assertIn('restrained number of natural discourse markers', payload['instructions'])
            self.assertIn('context-sensitive pauses', payload['instructions'])
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertFalse(report['web_fact_check'])

    def test_low_score_blocks_before_assets(self):
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        with tempfile.TemporaryDirectory() as tmp, patch.object(q.p, 'api_post', side_effect=[review_response(
                approved=False, score=6, severity='high'), review_response(approved=False, score=6, severity='high')]) as post:
            with self.assertRaisesRegex(ValueError, 'quality gate'):
                q.review_script(draft, {'idea':'A topic','minutes':1}, Path(tmp))
            self.assertFalse(json.loads((Path(tmp)/'script-quality.json').read_text())['approved'])
            self.assertEqual(post.call_count, 2)

    def test_inconsistent_approved_boolean_does_not_block_clean_final_script(self):
        response = review_response(approved=True)
        body = json.loads(response.json.return_value['output'][0]['content'][0]['text'])
        body['approved'] = False
        body['remaining_issues'] = [{'severity': 'low', 'reason': 'Optional stylistic preference.'}]
        response.json.return_value['output'][0]['content'][0]['text'] = json.dumps(body)
        with tempfile.TemporaryDirectory() as tmp, patch.object(q.p, 'api_post', return_value=response) as post:
            result = q.review_script('word ' * 100, {'idea': 'A topic', 'minutes': 1}, Path(tmp))
            self.assertTrue(result)
            self.assertEqual(post.call_count, 1)
            self.assertTrue(json.loads((Path(tmp)/'script-quality.json').read_text())['approved'])

    def test_approved_but_short_review_gets_one_length_correction(self):
        short = ' '.join(f'Short point {i} is useful.' for i in range(15))
        expanded = ' '.join(f'Expanded point {i} adds a clear useful detail.' for i in range(24))
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                q.p, 'api_post', side_effect=[review_response(script=short),
                                              review_response(script=expanded)]) as post:
            result = q.review_script(draft, {'idea': 'A topic', 'minutes': 1}, Path(tmp))
            self.assertEqual(result, expanded)
            self.assertEqual(post.call_count, 2)
            retry_payload = post.call_args_list[1].args[3]
            self.assertIn('failed the quality gate', retry_payload['instructions'])
            self.assertIn('required word range', retry_payload['instructions'])
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertTrue(report['correction_retry'])

    def test_word_count_only_failure_gets_one_extra_targeted_retry(self):
        near_minimum = ' '.join([f'Point {i} adds useful detail.' for i in range(18)] + ['One final point.'])
        in_range = ' '.join(f'Point {i} adds useful detail.' for i in range(19))
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                q.p, 'api_post', side_effect=[review_response(script=near_minimum),
                                              review_response(script=near_minimum),
                                              review_response(script=in_range)]) as post:
            result = q.review_script(draft, {'idea': 'A topic', 'minutes': 1}, Path(tmp))
            self.assertEqual(result, in_range.strip())
            self.assertEqual(post.call_count, 3)
            length_payload = post.call_args_list[2].args[3]
            self.assertIn('failed only the mechanical word-count check', length_payload['instructions'])
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertTrue(report['length_retry'])
            self.assertEqual(report['length_retry_attempts'], 1)

    def test_word_count_repair_retries_when_first_length_edit_is_still_short(self):
        short = ' '.join(f'Point {i} adds useful detail.' for i in range(18))
        nearly_long_enough = short + ' Still slightly short.'
        in_range = ' '.join(f'Point {i} adds useful detail.' for i in range(19))
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                q.p, 'api_post', side_effect=[review_response(script=short),
                                              review_response(script=short),
                                              review_response(script=nearly_long_enough),
                                              review_response(script=in_range)]) as post:
            result = q.review_script(draft, {'idea': 'A topic', 'minutes': 1}, Path(tmp))
            self.assertEqual(result, in_range)
            self.assertEqual(post.call_count, 4)
            final_payload = post.call_args_list[3].args[3]
            self.assertIn('currently contains exactly', final_payload['instructions'])
            self.assertIn('Count carefully', final_payload['instructions'])
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertEqual(report['length_retry_attempts'], 2)

    def test_overlong_sentence_is_split_locally_after_correction(self):
        long_sentence = 'This ' + ' '.join(f'detail{i}' for i in range(105)) + ', and it finishes clearly.'
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                q.p, 'api_post', side_effect=[review_response(script=long_sentence),
                                              review_response(script=long_sentence)]) as post:
            result = q.review_script(draft, {'idea': 'A topic', 'minutes': 1}, Path(tmp))
            self.assertEqual(post.call_count, 2)
            self.assertFalse(q.deterministic_findings(result, 1))
            self.assertEqual(len(result.split()), len(long_sentence.split()))
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertTrue(report['local_sentence_repair'])

    def test_sentence_splitter_uses_natural_boundary(self):
        sentence = ('One ' + ' '.join(f'word{i}' for i in range(30)) +
                    ', because ' + ' '.join(f'more{i}' for i in range(25)) + '.')
        repaired = q.split_overlong_sentences(sentence)
        self.assertNotIn('longer than 48 words', '; '.join(q.deterministic_findings(repaired, 1)))
        self.assertIn('. Because ', repaired)

    def test_deterministic_checks_reject_non_narration_format(self):
        findings = q.deterministic_findings('[excited]\nhttps://example.com ' + ('word ' * 70), 1)
        self.assertTrue(any('URL' in finding for finding in findings))
        self.assertTrue(any('heading' in finding for finding in findings))

    def test_stale_draft_length_complaint_does_not_reject_valid_final_script(self):
        final = ' '.join(f'Final point {i} adds a clear useful detail.' for i in range(24))
        response = review_response(approved=False, script=final, severity='high')
        body = json.loads(response.json.return_value['output'][0]['content'][0]['text'])
        body['remaining_issues'] = [{'severity': 'high', 'reason': 'Only 50 words instead of the required length.'}]
        response.json.return_value['output'][0]['content'][0]['text'] = json.dumps(body)
        with tempfile.TemporaryDirectory() as tmp, patch.object(q.p, 'api_post', return_value=response):
            result = q.review_script('word ' * 50, {'idea': 'A topic', 'minutes': 1}, Path(tmp))
            self.assertEqual(result, final)
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertTrue(report['recovered_stale_length_review'])
            self.assertEqual(report['remaining_issues'], [])

    def test_stale_length_recovery_never_ignores_other_review_issues(self):
        final = ' '.join(f'Final point {i} adds a clear useful detail.' for i in range(24))
        review = {'final_script': final,
                  'scores': {key: 8 for key in q.SCORE_KEYS},
                  'remaining_issues': [
                      {'severity': 'high', 'reason': 'The draft is too short.'},
                      {'severity': 'high', 'reason': 'A factual claim is unsupported.'}]}
        self.assertFalse(q.stale_length_conflict(review, 1))

class ScriptQualityResumeTests(unittest.TestCase):
    def test_reviewed_final_script_reuses_same_review(self):
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        final = ' '.join(f'Final point {i} adds a clear useful detail.' for i in range(24))
        request = {'idea': 'A topic', 'minutes': 1}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                q.p, 'api_post', return_value=review_response(script=final)) as post:
            root = Path(tmp)
            self.assertEqual(q.review_script(draft, request, root), final)
            self.assertEqual(q.review_script(final, request, root), final)
            self.assertEqual(post.call_count, 1)

    def test_recovers_redundant_failed_review_from_persisted_approval(self):
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        final = ' '.join(f'Final point {i} adds a clear useful detail.' for i in range(24))
        request = {'idea': 'A topic', 'minutes': 1}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                q.p, 'api_post', return_value=review_response(approved=False, score=6, script=final, severity='high')):
            root = Path(tmp); (root/'script.draft.txt').write_text(draft)
            (root/'idea.json').write_text(json.dumps({'narration': final, 'request': request}))
            with self.assertRaises(ValueError):
                q.review_script(final, request, root)
            recovered = q.review_script(final, request, root)
            report = json.loads((root/'script-quality.json').read_text())
            self.assertEqual(recovered, final)
            self.assertTrue(report['approved'])
            self.assertTrue(report['recovered_prior_approval'])
