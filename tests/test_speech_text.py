import unittest
from speech_text import prepare_speech_text, valid_cmu

class SpeechTextTests(unittest.TestCase):
    def test_prepares_numbers_and_cmu_without_chunking(self):
        value, report = prepare_speech_text("IMAX can project 70mm film at 24 fps.",
                                            [{"text": "IMAX", "cmu": "AY1 . M AE2 K S"}],
                                            apply_suggested_pronunciations=True)
        self.assertIn("<IMAX|AY1 . M AE2 K S>", value)
        self.assertIn("seventy millimeters", value)
        self.assertIn("twenty-four frames per second", value)
        self.assertEqual(report["pronunciations_applied"][0]["occurrences"], 1)

    def test_ai_name_pronunciations_are_advisory_only(self):
        names = [{"text": "Russell", "cmu": "R AH1 S AH0 L"},
                 {"text": "Rodgers", "cmu": "R AA1 JH ER0 Z"}]
        value, report = prepare_speech_text("Russell Wilson faced Aaron Rodgers.", names)
        self.assertEqual(value, "Russell Wilson faced Aaron Rodgers.")
        self.assertEqual(report["pronunciations_applied"], [])
        self.assertEqual(len(report["pronunciations_ignored"]), 2)

    def test_invalid_or_missing_annotations_are_ignored(self):
        value, report = prepare_speech_text("A normal sentence.", [{"text": "normal", "cmu": "NOT-A-PHONEME"}, {"text": "IMAX", "cmu": "AY1 M AE2 K S"}])
        self.assertEqual(value, "A normal sentence.")
        self.assertEqual(len(report["pronunciations_ignored"]), 2)

    def test_validates_arpabet(self):
        self.assertTrue(valid_cmu("AY1 . M AE2 K S"))
        self.assertFalse(valid_cmu("INVALID"))

class SpokenFormattingTests(unittest.TestCase):
    def test_currency_magnitude_places_dollars_last(self):
        value, report = prepare_speech_text("It cost $25 million, then another $1.5 billion and $1.")
        self.assertIn("twenty-five million dollars", value)
        self.assertIn("one point five billion dollars", value)
        self.assertIn("one dollar", value)
        self.assertNotIn("dollars million", value)

    def test_common_initialism_is_spelled_automatically(self):
        value, report = prepare_speech_text("GTA 5 became a major release.")
        self.assertIn("<GTA|JH IY1 . T IY1 . EY1> 5", value)
