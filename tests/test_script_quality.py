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
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertFalse(report['web_fact_check'])

    def test_low_score_blocks_before_assets(self):
        draft = ' '.join(f'Draft point {i} introduces a useful detail.' for i in range(24))
        with tempfile.TemporaryDirectory() as tmp, patch.object(q.p, 'api_post', return_value=review_response(
                approved=False, score=6, severity='high')):
            with self.assertRaisesRegex(ValueError, 'quality gate'):
                q.review_script(draft, {'idea':'A topic','minutes':1}, Path(tmp))
            self.assertFalse(json.loads((Path(tmp)/'script-quality.json').read_text())['approved'])

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
            self.assertIn('Expand it to at least 94 words', retry_payload['instructions'])
            report = json.loads((Path(tmp)/'script-quality.json').read_text())
            self.assertTrue(report['approved'])
            self.assertTrue(report['length_retry'])

    def test_deterministic_checks_reject_non_narration_format(self):
        findings = q.deterministic_findings('[excited]\nhttps://example.com ' + ('word ' * 70), 1)
        self.assertTrue(any('URL' in finding for finding in findings))
        self.assertTrue(any('heading' in finding for finding in findings))

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
