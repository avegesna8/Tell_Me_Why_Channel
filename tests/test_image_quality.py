import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import image_quality
import pipeline


class ImageQualityTests(unittest.TestCase):
    def test_ocr_keeps_only_confident_readable_words(self):
        tsv = ('level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
               '5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t96.2\tHELLO\n'
               '5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t30.0\tSCRIBBLE\n'
               '5\t1\t1\t1\t1\t3\t0\t0\t10\t10\t99.0\t--\n')
        completed = subprocess.CompletedProcess([], 0, tsv, '')
        with patch.object(image_quality.subprocess, 'run', return_value=completed):
            self.assertEqual(image_quality.ocr_image('scene.png'), [{'text': 'HELLO', 'confidence': 96.2}])

    def test_spelling_check_allows_words_names_and_short_ocr_noise(self):
        detected = [{'text': word, 'confidence': 90} for word in
                    ('Reach', 'YouTube', 'cul', 'Cluitural', 'Creatilvity')]
        suspicious = image_quality.suspicious_text(
            detected, allowed_text='A story about YouTube.', dictionary={'reach', 'story', 'about'})
        self.assertEqual([item['text'] for item in suspicious], ['Cluitural', 'Creatilvity'])

    def test_spelling_check_allows_hyphenated_name_from_prompt(self):
        detected = [{'text': 'SPIDER-MAN', 'confidence': 96}]
        suspicious = image_quality.suspicious_text(
            detected, allowed_text='A Spider-Man story.', dictionary={'a', 'story'})
        self.assertEqual(suspicious, [])

    def test_first_image_prompt_does_not_ban_lettering(self):
        board = {'title': 'Example', 'style_guide': 'Ink', 'aspect_ratio': '16:9',
                 'scenes': [{'id': 1, 'narration': 'Hello', 'prompt': 'A shop sign',
                             'camera_style': 'wide', 'mood': 'bright'}]}
        prompt = pipeline.image_prompt(board, board['scenes'][0])
        self.assertNotIn('added lettering', prompt)
        self.assertNotIn('absolutely no readable text', prompt)

    def test_spelling_retry_allows_text_but_demands_correct_spelling(self):
        prompt = pipeline.spelling_retry_prompt('Draw a theater.', [{'text': 'THEATR'}])
        self.assertIn('Visible text is allowed', prompt)
        self.assertIn('correctly spelled', prompt)
        self.assertNotIn('absolutely no readable text', prompt)

    def test_text_safe_fallback_avoids_text_bearing_objects(self):
        prompt = pipeline.text_safe_fallback_prompt('Draw an office.')
        self.assertIn('completely different visual concept', prompt)
        self.assertIn('Do not include signs, posters, books', prompt)
        self.assertIn('Absolutely no letters', prompt)

    def test_flagged_scene_retries_with_spelling_correction(self):
        failed = {'passed': False, 'detected_text': [{'text': 'WRNG', 'confidence': 91}],
                  'suspicious_text': [{'text': 'WRNG', 'confidence': 91}], 'image_sha256': 'old'}
        passed = {'passed': True, 'detected_text': [{'text': 'RIGHT'}],
                  'suspicious_text': [], 'image_sha256': 'new'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'old')
            payload = {'prompt': 'Draw a theater.', 'model': 'image'}
            with patch('image_quality.cached_scene', return_value=None), \
                 patch('image_quality.inspect', side_effect=[failed, passed]), \
                 patch.object(pipeline, 'request_image_bytes', return_value=b'new') as request:
                pipeline.check_image_text(image, payload, None, assets, 'scene-001', 'signature')
            request.assert_called_once()
            self.assertIn('Visible text is allowed', request.call_args.args[0]['prompt'])
            self.assertEqual(image.read_bytes(), b'new')
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertTrue(report['passed'])
            self.assertEqual(report['attempt'], 2)

    def test_spelling_retry_safety_rejection_uses_neutral_text_free_image(self):
        failed = {'passed': False, 'detected_text': [{'text': 'WRNG', 'confidence': 91}],
                  'suspicious_text': [{'text': 'WRNG', 'confidence': 91}], 'image_sha256': 'old',
                  'policy_version': 2}
        passed = {'passed': True, 'detected_text': [], 'suspicious_text': [],
                  'image_sha256': 'new', 'policy_version': 2}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'old')
            payload = {'prompt': 'Draw a trademarked subject.', 'model': 'image', 'size': '1536x1024'}
            with patch('image_quality.cached_scene', return_value=None), \
                 patch('image_quality.inspect', side_effect=[failed, passed]), \
                 patch.object(pipeline, 'request_image_bytes',
                              side_effect=[RuntimeError('rejected by the safety system'), b'new']) as request:
                pipeline.check_image_text(image, payload, b'reference', assets, 'scene-001', 'signature')
            self.assertEqual(request.call_count, 2)
            self.assertIsNone(request.call_args.args[1] if len(request.call_args.args) > 1 else None)
            self.assertNotIn('trademarked subject', request.call_args.args[0]['prompt'])
            self.assertTrue((assets / 'scene-001.prompt-text-safety-fallback.txt').exists())

    def test_second_misspelling_attempt_uses_text_safe_visual(self):
        failed = {'passed': False, 'attempt': 2, 'policy_version': 2, 'detected_text': [{'text': 'WRNG'}],
                  'suspicious_text': [{'text': 'WRNG'}],
                  'first_detected_text': [{'text': 'BAD'}],
                  'image_sha256': 'same', 'prompt_signature': 'signature'}
        passed = {'passed': True, 'detected_text': [], 'suspicious_text': [], 'image_sha256': 'fallback'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=failed), \
                 patch('image_quality.inspect', return_value=passed), \
                 patch.object(pipeline, 'request_image_bytes', return_value=b'fallback') as request:
                pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')
            request.assert_called_once()
            self.assertIn('completely different visual concept', request.call_args.args[0]['prompt'])
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertEqual(report['attempt'], 3)

    def test_legacy_cached_report_is_rechecked_under_spelling_policy_without_regeneration(self):
        legacy = {'passed': False, 'attempt': 3,
                  'detected_text': [{'text': 'cul'}, {'text': 'Reach'}, {'text': 'Flu'}],
                  'image_sha256': 'same', 'prompt_signature': 'signature'}
        passed = {'passed': True, 'detected_text': legacy['detected_text'],
                  'suspicious_text': [], 'image_sha256': 'same'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=legacy), \
                 patch('image_quality.inspect', return_value=passed), \
                 patch.object(pipeline, 'request_image_bytes') as request:
                pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')
            request.assert_not_called()
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertTrue(report['passed'])

    def test_old_spelling_report_is_rechecked_before_regeneration(self):
        old = {'passed': False, 'attempt': 1,
               'detected_text': [{'text': 'SPIDER-MAN'}],
               'suspicious_text': [{'text': 'SPIDER-MAN'}],
               'image_sha256': 'same', 'prompt_signature': 'signature'}
        passed = {'passed': True, 'detected_text': old['detected_text'],
                  'suspicious_text': [], 'image_sha256': 'same', 'policy_version': 2}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=old), \
                 patch('image_quality.inspect', return_value=passed) as inspect, \
                 patch.object(pipeline, 'request_image_bytes') as request:
                pipeline.check_image_text(
                    image, {'prompt': 'A Spider-Man story.'}, None, assets, 'scene-001', 'signature')
            inspect.assert_called_once()
            request.assert_not_called()
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertTrue(report['passed'])
            self.assertEqual(report['policy_version'], 2)

    def test_failed_text_safe_fallback_still_blocks_misspellings(self):
        failed = {'passed': False, 'attempt': 2, 'policy_version': 2, 'detected_text': [{'text': 'WRNG'}],
                  'suspicious_text': [{'text': 'WRNG'}],
                  'image_sha256': 'same', 'prompt_signature': 'signature'}
        still_failed = {'passed': False, 'detected_text': [{'text': 'Mispeling'}],
                        'suspicious_text': [{'text': 'Mispeling'}], 'image_sha256': 'new'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=failed), \
                 patch('image_quality.inspect', return_value=still_failed), \
                 patch.object(pipeline, 'request_image_bytes', return_value=b'fallback'):
                with self.assertRaisesRegex(RuntimeError, 'likely misspelled text'):
                    pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')


if __name__ == '__main__':
    unittest.main()
