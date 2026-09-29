import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation.production import metadata


def value(label):
    video = lambda kind: {
        'title': f'Why {label} {kind}?', 'description': f'{label} {kind} description',
        'tags': [label.lower(), kind], 'thumbnail_prompt': f'{label} {kind} image',
    }
    social = lambda name: {'caption': f'{label} {name} caption', 'hashtags': [label.lower()],
                           'cover_text': f'{label} FACT'}
    return {'long': video('long'), 'short': video('short'), 'platforms': {
        'facebook_long': {'title': f'Why {label}?', 'description': f'{label} facebook long'},
        'facebook_reel': social('facebook'), 'tiktok': social('tiktok'),
        'instagram': social('instagram'),
    }}


class SelectedMetadataTests(unittest.TestCase):
    def test_selected_replacement_preserves_other_format_metadata(self):
        for selected in ('long', 'short'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp); (project/'short').mkdir()
                for root, title in ((project, 'New long source'), (project/'short', 'New short source')):
                    (root/'storyboard.json').write_text(json.dumps({'title': title, 'scenes': []}))
                old, generated = value('OLD'), value('NEW')
                (project/'metadata.json').write_text(json.dumps(old))
                (project/'metadata-source.json').write_text(json.dumps({'signature': 'stale'}))
                response = Mock()
                response.json.return_value = {'status': 'completed', 'output': [{'content': [
                    {'type': 'output_text', 'text': json.dumps(generated)}]}]}
                with patch('automation.production.p.api_post', return_value=response):
                    result = metadata(project, replace_kind=selected)
                if selected == 'short':
                    self.assertEqual(result['long'], old['long'])
                    self.assertEqual(result['platforms']['facebook_long'], old['platforms']['facebook_long'])
                    self.assertEqual(result['short'], generated['short'])
                    self.assertEqual(result['platforms']['tiktok'], generated['platforms']['tiktok'])
                else:
                    self.assertEqual(result['short'], old['short'])
                    self.assertEqual(result['platforms']['instagram'], old['platforms']['instagram'])
                    self.assertEqual(result['long'], generated['long'])
                    self.assertEqual(result['platforms']['facebook_long'], generated['platforms']['facebook_long'])
                self.assertNotEqual(json.loads((project/'metadata-source.json').read_text())['signature'], 'stale')


if __name__ == '__main__':
    unittest.main()
