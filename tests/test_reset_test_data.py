import unittest

from automation.reset_test_data import video_ids


class ResetTestDataTests(unittest.TestCase):
    def test_collects_unique_recorded_upload_ids_only(self):
        state = {
            'first': {'uploads': {'long': {'id': 'long-id'}, 'short': {'id': 'short-id'}}},
            'second': {'uploads': {'long': {'id': 'long-id'}, 'short': {'started': True}}},
            'other': {'status': 'ready'},
        }
        self.assertEqual(video_ids(state), ['long-id', 'short-id'])
