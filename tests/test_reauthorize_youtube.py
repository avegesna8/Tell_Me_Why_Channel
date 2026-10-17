import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from automation.reauthorize_youtube import authorized_channel, discover_client_secrets, update_github_secrets


class YouTubeReauthorizationTests(unittest.TestCase):
    def test_discovers_newest_valid_desktop_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'client_secret_invalid.json').write_text('{}')
            valid = root/'client_secret_valid.json'
            valid.write_text(json.dumps({'installed': {'client_id': 'id'}}))
            self.assertEqual(discover_client_secrets(root), valid)

    def test_reads_authorized_channel(self):
        response = Mock()
        response.json.return_value = {'items': [{'id': 'channel-id', 'snippet': {'title': 'Tell Me Why'}}]}
        session = Mock(); session.get.return_value = response
        self.assertEqual(authorized_channel('access-token', session=session),
                         {'id': 'channel-id', 'title': 'Tell Me Why'})
        self.assertEqual(session.get.call_args.kwargs['headers']['Authorization'], 'Bearer access-token')

    def test_updates_three_secrets_through_stdin(self):
        credentials = SimpleNamespace(client_id='client-value-123', client_secret='private-value-456',
                                      refresh_token='refresh-value-789')
        run = Mock()
        update_github_secrets('owner/repository', credentials, run=run)
        self.assertEqual(run.call_count, 4)
        names = [call.args[0][3] for call in run.call_args_list[1:]]
        self.assertEqual(names, ['YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'YOUTUBE_REFRESH_TOKEN'])
        for call in run.call_args_list[1:]:
            self.assertNotIn(call.kwargs['input'], call.args[0])


if __name__ == '__main__':
    unittest.main()
