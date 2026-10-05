import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import production_review as review
from test_pipeline import board


def response(value, *, approved=True, score=8, severity='low'):
    visuals = [{key: scene.get(key, False if key == 'show_character' else 'medium')
                for key in ('id', 'prompt', 'camera_style', 'mood', 'motion', 'transition', 'show_character', 'image_quality')}
               for scene in value['scenes']]
    visuals[0]['prompt'] = 'A clearer scene-specific visual'
    body = {'approved': approved, 'scores': {name: score for name in review.SCORE_NAMES},
            'remaining_issues': [] if approved else [{'severity': severity, 'reason': 'Narration problem'}],
            'changes': ['Clarified scene one.'], 'pronunciations': [], 'final_visuals': visuals}
    result = Mock(); result.json.return_value = {'status': 'completed', 'output': [{'content': [
        {'type': 'output_text', 'text': json.dumps(body)}]}]}
    return result


class ProductionReviewTests(unittest.TestCase):
    def test_repairs_visuals_and_reuses_cache_after_saved_board_changes(self):
        value = board()
        with tempfile.TemporaryDirectory() as tmp, patch.object(review.p, 'api_post', return_value=response(value)) as post:
            first = review.review_storyboard(value, Path(tmp))
            second = review.review_storyboard(first, Path(tmp))
            self.assertEqual(first, second)
            self.assertEqual(first['scenes'][0]['prompt'], 'A clearer scene-specific visual')
            self.assertEqual(post.call_count, 1)
            self.assertIn('only in the final scene', post.call_args.args[3]['instructions'])
            self.assertTrue(json.loads((Path(tmp)/'production-review.json').read_text())['approved'])

    def test_high_severity_issue_blocks_assets(self):
        value = board()
        with tempfile.TemporaryDirectory() as tmp, patch.object(review.p, 'api_post', return_value=response(
                value, approved=False, score=6, severity='high')) as post:
            with self.assertRaisesRegex(ValueError, 'after two checks'):
                review.review_storyboard(value, Path(tmp))
            self.assertEqual(post.call_count, 2)
            report = json.loads((Path(tmp)/'production-review.json').read_text())
            self.assertEqual(report['attempts'], 2)
            self.assertFalse(report['approved'])

    def test_second_review_can_approve_repaired_plan(self):
        value = board()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                review.p, 'api_post', side_effect=[response(value, approved=False, score=7, severity='medium'),
                                                  response(value, approved=True, score=8)]) as post:
            result = review.review_storyboard(value, Path(tmp))
            self.assertEqual(post.call_count, 2)
            self.assertEqual(result['scenes'][0]['prompt'], 'A clearer scene-specific visual')
            report = json.loads((Path(tmp)/'production-review.json').read_text())
            self.assertEqual(report['attempts'], 2)
            self.assertTrue(report['approved'])
