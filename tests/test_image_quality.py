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

    def test_final_fallback_tolerates_one_short_substantial_match(self):
        self.assertTrue(image_quality.tolerable_fallback_text([
            {'text': 'cul'}, {'text': 'Reach'}, {'text': 'Flu'}]))
        self.assertFalse(image_quality.tolerable_fallback_text([
            {'text': 'Cultural'}, {'text': 'Influence'}]))
        self.assertFalse(image_quality.tolerable_fallback_text([
            {'text': 'Creatilvity'}]))

    def test_text_free_retry_prompt_forbids_typography(self):
        prompt = pipeline.text_free_retry_prompt('Draw a theater.')
        self.assertIn('absolutely no readable text', prompt)
        self.assertIn('blank color shapes', prompt)

    def test_text_safe_fallback_avoids_text_bearing_objects(self):
        prompt = pipeline.text_safe_fallback_prompt('Draw an office.')
        self.assertIn('completely different visual concept', prompt)
        self.assertIn('Do not include signs, posters, books', prompt)
        self.assertIn('Absolutely no letters', prompt)

    def test_flagged_scene_is_regenerated_only_once(self):
        failed = {'passed': False, 'detected_text': [{'text': 'WRNG', 'confidence': 91}],
                  'image_sha256': 'old'}
        passed = {'passed': True, 'detected_text': [], 'image_sha256': 'new'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'old')
            payload = {'prompt': 'Draw a theater.', 'model': 'image'}
            with patch('image_quality.cached_scene', return_value=None), \
                 patch('image_quality.inspect', side_effect=[failed, passed]), \
                 patch.object(pipeline, 'request_image_bytes', return_value=b'new') as request:
                pipeline.check_image_text(image, payload, None, assets, 'scene-001', 'signature')
            request.assert_called_once()
            self.assertEqual(image.read_bytes(), b'new')
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertTrue(report['passed'])
            self.assertEqual(report['attempt'], 2)

    def test_second_failed_attempt_uses_different_text_safe_visual(self):
        failed = {'passed': False, 'attempt': 2, 'detected_text': [{'text': 'WRNG'}],
                  'first_detected_text': [{'text': 'BAD'}],
                  'image_sha256': 'same', 'prompt_signature': 'signature'}
        passed = {'passed': True, 'detected_text': [], 'image_sha256': 'fallback'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=failed), \
                 patch('image_quality.inspect', return_value=passed), \
                 patch.object(pipeline, 'request_image_bytes', return_value=b'fallback') as request:
                pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')
            request.assert_called_once()
            self.assertIn('completely different visual concept', request.call_args.args[0]['prompt'])
            self.assertTrue((assets / 'scene-001.prompt-text-fallback.txt').exists())
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertEqual(report['attempt'], 3)
            self.assertEqual(report['first_detected_text'], [{'text': 'BAD'}])
            self.assertEqual(report['second_detected_text'], [{'text': 'WRNG'}])

    def test_cached_third_attempt_accepts_weak_ocr_without_new_image_request(self):
        weak = {'passed': False, 'attempt': 3,
                'detected_text': [{'text': 'cul'}, {'text': 'Reach'}, {'text': 'Flu'}],
                'image_sha256': 'same', 'prompt_signature': 'signature'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=weak), \
                 patch.object(pipeline, 'request_image_bytes') as request:
                pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')
            request.assert_not_called()
            report = json.loads((root / 'image-quality.json').read_text())['scenes']['scene-001']
            self.assertTrue(report['passed'])
            self.assertTrue(report['accepted_ocr_warning'])

    def test_failed_text_safe_fallback_still_blocks(self):
        failed = {'passed': False, 'attempt': 2, 'detected_text': [{'text': 'WRNG'}],
                  'image_sha256': 'same', 'prompt_signature': 'signature'}
        still_failed = {'passed': False, 'detected_text': [{'text': 'NOPE'}, {'text': 'WORDS'}], 'image_sha256': 'new'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=failed), \
                 patch('image_quality.inspect', return_value=still_failed), \
                 patch.object(pipeline, 'request_image_bytes', return_value=b'fallback'):
                with self.assertRaisesRegex(RuntimeError, 'substantial readable text'):
                    pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')


if __name__ == '__main__':
    unittest.main()
