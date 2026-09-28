from array import array
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
import wave

from narration_quality import apply_final_attempt_policy, cached_report, inspect, transcript_match, wav_signal


class NarrationQualityTests(unittest.TestCase):
    def make_wav(self, path, *, seconds=2, amplitude=5000, silence=None):
        rate = 16000
        samples = array('h')
        for index in range(round(rate * seconds)):
            value = int(amplitude * math.sin(2 * math.pi * 220 * index / rate))
            if silence and silence[0] * rate <= index < silence[1] * rate:
                value = 0
            samples.append(value)
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
            output.writeframes(samples.tobytes())

    def test_phoneme_markup_compares_visible_term(self):
        fraction, wanted, heard = transcript_match(
            'Play <GTA|JH IY1 . T IY1 . EY1> five today.',
            [{'word': word} for word in 'Play GTA five today'.split()])
        self.assertEqual(fraction, 1)
        self.assertEqual(wanted, heard)

    def test_initialisms_and_typographic_contractions_compare_cleanly(self):
        fraction, wanted, heard = transcript_match(
            "The U.S. isn't changing today’s plan.",
            [{'word': word} for word in "The US isn't changing today's plan".split()])
        self.assertEqual(fraction, 1)
        self.assertEqual(wanted, heard)


    def test_repeated_phrase_lowers_match_score(self):
        recognized = [{'word': word} for word in 'one two two two three'.split()]
        fraction, _, _ = transcript_match('one two three', recognized)
        self.assertLess(fraction, .88)

    def test_clean_matching_audio_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / 'voice.wav'; self.make_wav(audio)
            report = inspect(audio, 'Hello clear world', transcriber=lambda *_: [
                {'word': 'Hello', 'start': 0, 'end': .4}, {'word': 'clear', 'start': .5, 'end': .9},
                {'word': 'world', 'start': 1, 'end': 1.4}])
            self.assertTrue(report['passed'])
            self.assertEqual(report['matched_fraction'], 1)

    def test_borderline_transcript_passes_when_no_phrase_or_signal_check_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / 'voice.wav'; self.make_wav(audio)
            expected = ' '.join(f'word{i}' for i in range(50))
            omitted = {4, 11, 18, 25, 32, 39, 46}
            heard = [f'word{i}' for i in range(50) if i not in omitted]
            report = inspect(audio, expected, transcriber=lambda *_: [
                {'word': word, 'start': 0, 'end': .1} for word in heard])
            self.assertTrue(report['passed'])
            self.assertAlmostEqual(report['matched_fraction'], .86)
            self.assertTrue(report['warnings'])

    def test_cached_borderline_report_is_upgraded_without_retranscribing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'voice.wav'; self.make_wav(audio)
            expected = 'A complete intended narration'
            report_path = root / 'narration-quality.json'
            report_path.write_text(json.dumps({
                'passed': False,
                'matched_fraction': .86,
                'failures': ['Whisper matched only 86% of the intended narration'],
                'warnings': [],
                'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
                'expected_sha256': hashlib.sha256(expected.encode()).hexdigest(),
                'whisper_model': 'small',
            }))
            report = cached_report(report_path, audio, expected, 'small')
            self.assertTrue(report['passed'])
            self.assertEqual(report['policy_version'], 3)
            self.assertEqual(report['failures'], [])
            self.assertTrue(report['warnings'])

    def test_final_attempt_accepts_marginal_match_only_without_other_failures(self):
        marginal = {'passed': False, 'attempt': 3, 'matched_fraction': .84,
                    'failures': ['Whisper matched only 84% of the intended narration'], 'warnings': []}
        self.assertTrue(apply_final_attempt_policy(marginal)['passed'])
        structural = {'passed': False, 'attempt': 3, 'matched_fraction': .84,
                      'failures': ['Whisper matched only 84% of the intended narration',
                                   'a phrase of 6 intended words was missing or garbled'], 'warnings': []}
        self.assertFalse(apply_final_attempt_policy(structural)['passed'])

    def test_cached_final_attempt_is_upgraded_without_retranscribing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / 'voice.wav'; self.make_wav(audio)
            expected = 'A complete intended narration'
            report_path = root / 'narration-quality.json'
            report_path.write_text(json.dumps({
                'passed': False, 'attempt': 3, 'matched_fraction': .84,
                'failures': ['Whisper matched only 84% of the intended narration'], 'warnings': [],
                'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
                'expected_sha256': hashlib.sha256(expected.encode()).hexdigest(),
                'whisper_model': 'small', 'policy_version': 2,
            }))
            report = cached_report(report_path, audio, expected, 'small')
            self.assertTrue(report['passed'])
            self.assertEqual(report['policy_version'], 3)

    def test_bad_transcript_and_internal_silence_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / 'voice.wav'; self.make_wav(audio, seconds=5, silence=(1, 4))
            report = inspect(audio, 'The intended narration has several words',
                             transcriber=lambda *_: [{'word': 'unrelated', 'start': 0, 'end': 1}])
            self.assertFalse(report['passed'])
            self.assertGreaterEqual(len(report['failures']), 2)

    def test_long_garbled_phrase_fails_even_when_total_match_is_high(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / 'voice.wav'; self.make_wav(audio)
            expected = ' '.join(['clear'] * 50 + ['allowing corruption to spread under his administration'] + ['clear'] * 50)
            heard = ' '.join(['clear'] * 50 + ['unrelated sounds'] + ['clear'] * 50)
            report = inspect(audio, expected, transcriber=lambda *_: [
                {'word': word, 'start': 0, 'end': .1} for word in heard.split()])
            self.assertFalse(report['passed'])
            self.assertTrue(any('phrase of' in failure for failure in report['failures']))

    def test_signal_reports_clipping(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / 'voice.wav'; self.make_wav(audio, amplitude=32767)
            self.assertGreater(wav_signal(audio)['clipped_sample_fraction'], 0.005)


class NarrationRetryTests(unittest.TestCase):
    def test_failed_first_check_retries_once_and_saves_passing_report(self):
        import json
        from unittest.mock import Mock, patch
        import pipeline
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            audio = assets / 'narration.wav'
            NarrationQualityTests().make_wav(audio)
            retry_audio = audio.read_bytes()
            runpod = Mock(); runpod.synthesize.return_value = retry_audio
            base = {'audio_sha256': 'first', 'whisper_model': 'small', 'matched_fraction': .4,
                    'signal': {}, 'warnings': [], 'failures': ['mismatch'], 'words': []}
            passed = {**base, 'audio_sha256': 'second', 'matched_fraction': 1, 'passed': True,
                      'failures': [], 'words': [{'word': 'Hello', 'start': 0, 'end': 1}]}
            failed = {**base, 'passed': False}
            with patch('narration_quality.cached_report', return_value=None), \
                 patch('narration_quality.inspect', side_effect=[failed, passed]):
                pipeline.check_runpod_narration(audio, 'Hello', assets, runpod, 'signature')
            runpod.synthesize.assert_called_once()
            report = json.loads((root / 'narration-quality.json').read_text())
            self.assertTrue(report['passed'])
            self.assertEqual(report['attempt'], 2)
            self.assertTrue((assets / 'narration.runpod-retry-1-job.json').parent.exists())

    def test_cached_second_failure_requests_unique_third_attempt(self):
        import json
        from unittest.mock import Mock, patch
        import pipeline
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); assets = root / 'assets'; assets.mkdir()
            audio = assets / 'part.wav'
            NarrationQualityTests().make_wav(audio)
            failed = {'passed': False, 'attempt': 2, 'audio_sha256': 'second',
                      'expected_sha256': 'expected', 'whisper_model': 'small',
                      'matched_fraction': .7, 'signal': {}, 'warnings': [],
                      'failures': ['mismatch'], 'mismatches': []}
            passed = {**failed, 'passed': True, 'matched_fraction': .96,
                      'failures': [], 'words': [{'word': 'Hello', 'start': 0, 'end': 1}],
                      'audio_sha256': 'third'}
            runpod = Mock(); runpod.synthesize.return_value = audio.read_bytes()
            with patch('narration_quality.cached_report', return_value=failed), \
                 patch('narration_quality.inspect', return_value=passed):
                pipeline.check_runpod_narration(audio, 'Hello', assets, runpod, 'signature', report_stem='part')
            job_path = runpod.synthesize.call_args.args[1]
            self.assertEqual(job_path.name, 'part.runpod-retry-2-job.json')
            self.assertEqual(json.loads((root/'part-quality.json').read_text())['attempt'], 3)


if __name__ == '__main__':
    unittest.main()
