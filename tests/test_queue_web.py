import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import queue_web as q


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, text=True, capture_output=True)


class QueueWebTests(unittest.TestCase):
    def test_add_commit_push_and_idempotent_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); origin = base/'origin.git'; repo = base/'repo'
            git(base, 'init', '--bare', str(origin))
            git(base, 'init', '-b', 'main', str(repo))
            git(repo, 'config', 'user.email', 'test@example.com'); git(repo, 'config', 'user.name', 'Test')
            queue = {'email_mode':'review','category_id':'27','items':[],
                     'timezone':'America/New_York','notification_email':'test@example.com'}
            (repo/'video_queue.json').write_text(json.dumps(queue))
            (repo/'.gitignore').write_text('.queue-web.lock\n')
            git(repo, 'add', 'video_queue.json', '.gitignore'); git(repo, 'commit', '-m', 'Initial queue')
            git(repo, 'remote', 'add', 'origin', str(origin)); git(repo, 'push', '-u', 'origin', 'main')
            payload = {'request_id':'12345678-1234-5678-9234-567812345678', 'idea':'Why is the sky blue?',
                       'notes':'Keep it clear.', 'minutes':2, 'short_seconds':45}
            with patch.object(q, 'ROOT', repo), patch.object(q, 'QUEUE', repo/'video_queue.json'):
                first = q.add_idea(payload); second = q.add_idea(payload)
            saved = json.loads((repo/'video_queue.json').read_text())
            self.assertEqual(len(saved['items']), 1)
            self.assertEqual(saved['items'][0]['id'], 'why-is-the-sky-blue-12345678')
            self.assertTrue(first['committed']); self.assertTrue(second['already_present'])
            self.assertEqual(git(repo, 'rev-list', '--count', 'main').stdout.strip(), '2')
            self.assertEqual(git(repo, 'status', '--porcelain').stdout, '')
