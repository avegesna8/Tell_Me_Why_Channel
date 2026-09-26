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

    def test_text_free_retry_prompt_forbids_typography(self):
        prompt = pipeline.text_free_retry_prompt('Draw a theater.')
        self.assertIn('absolutely no readable text', prompt)
        self.assertIn('blank color shapes', prompt)

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

    def test_second_failed_attempt_blocks_without_another_request(self):
        failed = {'passed': False, 'attempt': 2, 'detected_text': [{'text': 'WRNG'}],
                  'image_sha256': 'same', 'prompt_signature': 'signature'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            image = assets / 'scene-001.png'; image.write_bytes(b'image')
            with patch('image_quality.cached_scene', return_value=failed), \
                 patch.object(pipeline, 'request_image_bytes') as request:
                with self.assertRaisesRegex(RuntimeError, 'still contains readable text'):
                    pipeline.check_image_text(image, {'prompt': 'draw'}, None, assets, 'scene-001', 'signature')
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
