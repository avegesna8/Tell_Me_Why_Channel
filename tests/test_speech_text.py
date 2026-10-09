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

    def test_em_dash_becomes_speech_only_sentence_break(self):
        value, report = prepare_speech_text("It sounds simple—but there is a catch.")
        self.assertEqual(value, "It sounds simple. But there is a catch.")
        self.assertIn({"written": "—b", "spoken": ". B"}, report["normalizations"])

    def test_common_initialism_is_spelled_automatically(self):
        value, report = prepare_speech_text("GTA 5 became a major release.")
        self.assertIn("<GTA|JH IY1 . T IY1 . EY1> 5", value)

    def test_gym_pronunciation_handles_sentence_case(self):
        value, report = prepare_speech_text("Gym access makes the gym convenient.")
        self.assertIn("<Gym|JH IH1 M>", value)
        self.assertIn("<gym|JH IH1 M>", value)
        self.assertEqual(report["pronunciations_applied"], [{
            "text": "gym", "cmu": "JH IH1 M", "occurrences": 2}])

    def test_known_tts_mispronunciation_is_annotated_without_changing_visible_script(self):
        value, report = prepare_speech_text("The pricier ticket may be worthwhile.")
        self.assertIn("<pricier|P R AY1 S IY0 ER0>", value)
        self.assertEqual(report["pronunciations_applied"], [{
            "text": "pricier", "cmu": "P R AY1 S IY0 ER0", "occurrences": 1}])

    def test_quoted_orange_words_are_unquoted_and_pronounced_for_speech_only(self):
        script = 'The color “orange” came after the fruit "oranges."'
        value, report = prepare_speech_text(script)
        self.assertNotIn('“', value)
        self.assertNotIn('”', value)
        self.assertNotIn('"', value)
        self.assertIn('<orange|AO1 R AH0 N JH>', value)
        self.assertIn('<oranges|AO1 R AH0 N JH IH0 Z>.', value)
        self.assertEqual(script, 'The color “orange” came after the fruit "oranges."')
        self.assertEqual(len(report['normalizations']), 2)
