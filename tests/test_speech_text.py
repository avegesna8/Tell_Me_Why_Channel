import unittest
from speech_text import prepare_speech_text, valid_cmu

class SpeechTextTests(unittest.TestCase):
    def test_prepares_numbers_and_cmu_without_chunking(self):
        value, report = prepare_speech_text("IMAX can project 70mm film at 24 fps.", [{"text": "IMAX", "cmu": "AY1 . M AE2 K S"}])
        self.assertIn("<IMAX|AY1 . M AE2 K S>", value)
        self.assertIn("seventy millimeters", value)
        self.assertIn("twenty-four frames per second", value)
        self.assertEqual(report["pronunciations_applied"][0]["occurrences"], 1)

    def test_invalid_or_missing_annotations_are_ignored(self):
        value, report = prepare_speech_text("A normal sentence.", [{"text": "normal", "cmu": "NOT-A-PHONEME"}, {"text": "IMAX", "cmu": "AY1 M AE2 K S"}])
        self.assertEqual(value, "A normal sentence.")
        self.assertEqual(len(report["pronunciations_ignored"]), 2)

    def test_validates_arpabet(self):
        self.assertTrue(valid_cmu("AY1 . M AE2 K S"))
        self.assertFalse(valid_cmu("INVALID"))
