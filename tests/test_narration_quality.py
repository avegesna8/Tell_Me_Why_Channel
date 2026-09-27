from array import array
import math
from pathlib import Path
import tempfile
import unittest
import wave

from narration_quality import inspect, transcript_match, wav_signal


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
            self.assertTrue((assets / 'narration.runpod-retry-job.json').parent.exists())


if __name__ == '__main__':
    unittest.main()
