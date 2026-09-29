import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import shorts as s


def board():
    return {'title': 'Topic', 'style_guide': 'Ink drawings', 'aspect_ratio': '16:9', 'scenes': [
        {'id': 1, 'narration': 'A surprising fact.', 'prompt': 'A diagram', 'camera_style': 'Wide',
         'mood': 'Curious', 'estimated_duration': 5, 'motion': 'still', 'transition': 'cut'}]}


class ShortsTests(unittest.TestCase):
    def test_script_resume_and_source_invalidation(self):
        response = Mock()
        response.json.return_value = {'status': 'completed', 'output': [{'content': [
            {'type': 'output_text', 'text': json.dumps({'title': 'Short', 'narration': 'Why does this happen?'})}]}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(s.p, 'api_post', return_value=response) as api:
            root = Path(tmp)
            path = s.short_script(board(), root, 55)
            self.assertEqual(s.short_script(board(), root, 55), path)
            self.assertEqual(api.call_count, 1)
            changed = board(); changed['scenes'][0]['narration'] = 'Different source.'
            with self.assertRaisesRegex(ValueError, 'source or target'):
                s.short_script(changed, root, 55)
            with self.assertRaises(ValueError):
                s.short_script(board(), root, 60)

    def test_empty_regeneration_notes_keep_legacy_resume_signature(self):
        response = Mock()
        response.json.return_value = {'status': 'completed', 'output': [{'content': [
            {'type': 'output_text', 'text': json.dumps({'title': 'Short', 'narration': 'Complete thought.'})}]}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(s.p, 'api_post', return_value=response) as api:
            root = Path(tmp)
            first = s.short_script(board(), root, 30)
            self.assertEqual(s.short_script(board(), root, 30, ''), first)
            self.assertEqual(json.loads((root/'short-script.json').read_text())['request']['version'], 1)
            self.assertEqual(api.call_count, 1)

    def test_duration_adjustment_never_truncates(self):
        self.assertEqual(s.fit_speed(52, 55), 1)
        self.assertAlmostEqual(s.fit_speed(65, 55), 65 / 55)
        self.assertAlmostEqual(s.fit_speed(44, 55), .8)
        self.assertAlmostEqual(s.fit_speed(44.8, 60), .8)
        for length in [0, float('nan'), 20, 100]:
            with self.assertRaises(ValueError):
                s.fit_speed(length, 55)

    def test_separate_vertical_assets_and_continuous_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); short = root / 'short'; short.mkdir()
            script = short / 'script.txt'; script.write_text('A surprising fact.')
            def plan(text, path, count, style, aspect):
                value = board(); value['aspect_ratio'] = aspect
                path.write_text(json.dumps(value))
            with patch.object(s, 'short_script', return_value=script), patch.object(s.p, 'plan', side_effect=plan), \
                 patch('production_review.review_storyboard', side_effect=lambda value, directory: value), \
                 patch.object(s.p, 'generate_assets') as assets, patch.object(s.p, 'audio_asset', return_value=short/'narration.wav'), \
                 patch.object(s.p, 'duration', side_effect=[65, 55]), patch.object(s.p, 'render') as render:
                s.create_short(board(), root, image_reference=Path('characters/updated_base.png'))
            self.assertEqual(assets.call_args.args[1], short / 'assets')
            self.assertEqual(assets.call_args.args[0]['aspect_ratio'], '9:16')
            self.assertEqual(render.call_args.kwargs['narration_mode'], 'continuous')
            self.assertAlmostEqual(render.call_args.kwargs['narration_speed'], 65/55)
            self.assertEqual(render.call_args.kwargs['ending_padding'], 0.5)
            self.assertFalse((root / 'storyboard.json').exists())
            self.assertTrue((short / 'short-timing.json').exists())

    def test_thirty_second_target_preserves_natural_complete_audio(self):
        self.assertEqual(s.duration_bounds(30), (20, 50))
        self.assertEqual(s.fit_speed(33, 30), 1)
        self.assertEqual(s.fit_speed(27, 30), 1)
        self.assertEqual(s.fit_speed(45, 30), 1)
        self.assertEqual(s.fit_speed(20, 30), 1)
        self.assertAlmostEqual(s.fit_speed(55, 30), 55 / 50)
        for length in [15, 60]:
            with self.assertRaises(ValueError):
                s.fit_speed(length, 30)
