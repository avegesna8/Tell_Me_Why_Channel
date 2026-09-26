import base64
import json
import unittest
from unittest.mock import Mock, patch

from automation.archive_queue import archive_completed, remove_item


class ArchiveQueueTests(unittest.TestCase):
    def test_remove_only_completed_item(self):
        raw = json.dumps({'email_mode': 'review', 'items': [
            {'id': 'done', 'idea': 'Done', 'made_for_kids': False, 'contains_synthetic_media': False},
            {'id': 'next', 'idea': 'Next', 'made_for_kids': False, 'contains_synthetic_media': False}]})
        updated = json.loads(remove_item(raw, 'done'))
        self.assertEqual([item['id'] for item in updated['items']], ['next'])
        self.assertIsNone(remove_item(json.dumps(updated), 'done'))

    def test_atomic_github_update_retries_conflict(self):
        queue = {'email_mode': 'review', 'items': [
            {'id': 'done', 'idea': 'Done', 'made_for_kids': False, 'contains_synthetic_media': False}]}
        get = Mock(ok=True, status_code=200)
        encoded = base64.b64encode(json.dumps(queue).encode()).decode()
        wrapped = '\n'.join(encoded[index:index+20] for index in range(0, len(encoded), 20))
        get.json.side_effect = [
            {'sha': 'old', 'content': wrapped},
            {'sha': 'new', 'content': wrapped}]
        conflict = Mock(ok=False, status_code=409); success = Mock(ok=True, status_code=200)
        with patch('automation.archive_queue.requests.get', return_value=get) as reads, \
             patch('automation.archive_queue.requests.put', side_effect=[conflict, success]) as writes:
            self.assertTrue(archive_completed('owner/repo', 'video_queue.json', 'done', 'token'))
        self.assertEqual(reads.call_count, 2); self.assertEqual(writes.call_count, 2)
        body = writes.call_args.kwargs['json']
        self.assertEqual(body['sha'], 'new')
        self.assertEqual(json.loads(base64.b64decode(body['content']))['items'], [])
