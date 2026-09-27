
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.production import social_covers


class SocialCoverTests(unittest.TestCase):
    def test_social_covers_use_platform_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project/'short/assets/scene-001.png'
            source.parent.mkdir(parents=True); source.write_bytes(b'image')
            platforms = {name: {'cover_text': text} for name, text in {
                'facebook_reel': 'Facebook Answer', 'instagram': 'Instagram Answer',
                'tiktok': 'TikTok Answer'}.items()}
            def render(args):
                Path(args[-1]).write_bytes(b'jpeg')
            with patch.dict('os.environ', {'COVER_FONT': '/tmp/test-font.ttf'}), \
                 patch('automation.production.p.run_ffmpeg', side_effect=render):
                covers = social_covers(project, platforms)
            self.assertEqual(set(covers), {'facebook', 'instagram', 'tiktok'})
            self.assertTrue(all(path.exists() for path in covers.values()))
            self.assertFalse(any((project/'short/covers').glob('.*-cover-text.txt')))


if __name__ == '__main__':
    unittest.main()
