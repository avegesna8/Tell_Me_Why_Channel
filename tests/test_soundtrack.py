import array
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch, Mock

import pipeline as p
import soundtrack as s


class SoundtrackTests(unittest.TestCase):
    def test_library_blocks_unknown_licenses_and_escaping_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'library.json'
            entry={'id':'track','file':'track.wav','attribution_required':False,'sha256':'a'*64}
            for change in ({'file':'../outside.wav'},{'attribution_required':None},
                           {'attribution_required':True,'attribution_text':None}):
                path.write_text(json.dumps({'music':[{**entry,**change}]}))
                with self.assertRaises(ValueError): s.load_library(path,check_files=False)

    def test_cues_resolve_to_words_and_reject_dense_or_invented_effects(self):
        entries=[{'id':'music','kind':'music','attribution_required':True,'attribution_text':'Music credit'},
                 {'id':'pop','kind':'sfx','attribution_required':False,'clip_seconds':1},
                 {'id':'siren','kind':'sfx','attribution_required':False,'clip_seconds':1}]
        words=[{'word':'Look','start':1},{'word':'bigger','start':3},{'word':'screen','start':20}]
        plan={'music_id':'music','effects':[{'effect_id':'pop','word_index':2}]}
        result=s.resolve_plan(plan,entries,words,60)
        self.assertEqual(result['effects'][0]['at_seconds'],20)
        self.assertEqual(result['credits'],['Music credit'])
        for effects in ([{'effect_id':'unknown','word_index':0}],
                        [{'effect_id':'pop','word_index':90}],
                        [{'effect_id':'siren','word_index':0}]):
            corrected = s.resolve_plan({**plan,'effects':effects},entries,words,60)
            self.assertEqual(corrected['effects'], [])
            self.assertTrue(corrected['skipped_effects'])
        dense = s.resolve_plan({**plan, 'effects': [{'effect_id':'pop','word_index':i} for i in (0,1)]}, entries, words, 60)
        self.assertEqual(len(dense['effects']), 1)
        self.assertEqual(dense['effects'][0]['word_index'], 0)
        self.assertIn('twelve seconds', dense['skipped_effects'][0]['reason'])

    def test_cached_plan_makes_no_new_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'storyboard.json').write_text(json.dumps({'title':'Test','scenes':[]}))
            (root/'alignment.json').write_text(json.dumps({'words':[{'word':'Hello','start':0,'end':1}]}))
            entries=[{'id':'music','kind':'music','path':root/'music.wav','attribution_required':False},
                     {'id':'pop','kind':'sfx','path':root/'pop.wav','attribution_required':False}]
            response=Mock();response.json.return_value={'status':'completed','output':[{'content':[
                {'type':'output_text','text':json.dumps({'music_id':'music','effects':[]})}]}]}
            with patch.object(p,'api_post',return_value=response) as api, patch.object(p,'duration',return_value=30):
                self.assertEqual(s.make_plan(root,entries),s.make_plan(root,entries))
                self.assertEqual(api.call_count,1)
                s.make_plan(root,entries+[{'id':'new_track','kind':'music','path':root/'new.wav','attribution_required':False}])
                self.assertEqual(api.call_count,1)

    def test_audio_regeneration_retimes_cached_plan_without_new_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'storyboard.json').write_text(json.dumps({'title':'Test','scenes':[]}))
            alignment=root/'alignment.json'
            alignment.write_text(json.dumps({'words':[{'word':'Reveal','start':2,'end':3}]}))
            entries=[{'id':'pop','kind':'sfx','path':root/'pop.wav','attribution_required':False,
                      'clip_seconds':1}]
            response=Mock();response.json.return_value={'status':'completed','output':[{'content':[
                {'type':'output_text','text':json.dumps({'music_id':None,'effects':[
                    {'effect_id':'pop','word_index':0}]})}]}]}
            with patch.object(p,'api_post',return_value=response) as api, patch.object(p,'duration',return_value=30):
                original=s.make_plan(root,entries)
                self.assertEqual(original['effects'][0]['at_seconds'],2)
                alignment.write_text(json.dumps({'words':[{'word':'Reveal','start':7,'end':8}]}))
                with patch.dict('os.environ', {'REUSE_SOUNDTRACK_PLAN':'1'}):
                    retimed=s.make_plan(root,entries)
                self.assertEqual(retimed['effects'][0]['at_seconds'],7)
                self.assertEqual(api.call_count,1)
            saved=json.loads((root/'soundtrack-plan.json').read_text())
            self.assertTrue(saved['retimed'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_real_mix_keeps_music_steady_preserves_duration_and_resumes_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            p.run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=160x90:r=10:d=8',
                '-f','lavfi','-i',"aevalsrc=if(between(t\\,2\\,4)\\,0.4*sin(2*PI*4000*t)\\,0):s=48000:d=8",
                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(root/'video.mp4')])
            for name,hz in [('music',180),('pop',2000)]:
                p.run_ffmpeg(['-f','lavfi','-i',f'sine=frequency={hz}:duration=8:sample_rate=48000',str(root/f'{name}.wav')])
            entries=[{'id':name,'kind':'music' if name=='music' else 'sfx','path':root/f'{name}.wav',
                      'file':f'{name}.wav','sha256':s.file_hash(root/f'{name}.wav')} for name in ('music','pop')]
            plan={'music_id':'music','credits':[], 'effects':[{'effect_id':'pop','word_index':0,
                  'word':'test','at_seconds':6,'source_start':0,'clip_seconds':1}]}
            original=s.file_hash(root/'video.mp4')
            replace=Path.replace
            def interrupted(path,target):
                if path.name=='soundtrack.partial.mp4': raise OSError('simulated interruption')
                return replace(path,target)
            with patch.object(Path,'replace',interrupted):
                with self.assertRaises(OSError): s.mix_video(root,entries,plan)
            self.assertEqual(s.file_hash(root/'video.mp4'),original)
            s.mix_video(root,entries,plan)
            self.assertAlmostEqual(p.duration(root/'video.mp4'),8,places=1)
            self.assertEqual(s.file_hash(root/'video.narration.mp4'),original)
            self.assertEqual(json.loads((root/'soundtrack.json').read_text())['mix']['music_lufs'], -44)
            mixed=s.file_hash(root/'video.mp4')
            with patch.object(p,'run_ffmpeg') as render:
                s.mix_video(root,entries,plan)
                render.assert_not_called()
            self.assertEqual(s.file_hash(root/'video.mp4'),mixed)
            data=subprocess.run(['ffmpeg','-v','error','-i',str(root/'video.mp4'),'-vn','-af',
                'bandpass=f=180:t=h:w=20','-f','f32le','-ac','1','-ar','48000','-'],capture_output=True,check=True).stdout
            samples=array.array('f',data)
            def rms(start,end):
                values=samples[int(start*48000):int(end*48000)]
                return math.sqrt(sum(v*v for v in values)/len(values))
            quiet_music = rms(1.0,1.5)
            under_voice = rms(2.8,3.3)
            after_voice = rms(5,5.5)
            self.assertGreater(under_voice, quiet_music*.85)
            self.assertLess(under_voice, quiet_music*1.15)
            self.assertGreater(after_voice, quiet_music*.85)
            self.assertLess(after_voice, quiet_music*1.15)
            with patch.object(p,'run_ffmpeg') as render:
                s.mix_video(root,entries,{'music_id':None,'effects':[],'credits':[]})
                render.assert_not_called()
            self.assertEqual(s.file_hash(root/'video.mp4'),original)
            s.mix_video(root,entries,{**plan,'music_id':None})
            self.assertAlmostEqual(p.duration(root/'video.mp4'),8,places=1)


    def test_new_local_audio_is_discovered_without_catalog_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'music').mkdir()
            with wave.open(str(root/'music'/'Fresh Calm Track.wav'),'wb') as wav:
                wav.setparams((1,2,8000,0,'NONE','not compressed'))
                wav.writeframes(b'\0\0'*8000)
            path=root/'library.json';path.write_text('{"music":[],"sfx":[]}')
            entries=s.load_library(path)
            self.assertEqual(len(entries),1)
            self.assertEqual(entries[0]['title'],'Fresh Calm Track')
            self.assertEqual(entries[0]['kind'],'music')

    def test_checked_in_catalog_lists_the_managed_music_collection(self):
        catalog=json.loads(Path('audio/library.json').read_text())
        by_file={entry['file']:entry for entry in catalog['music']}
        expected={
            'music/Tiburtina - Schwartzy.mp3': {'ambient','calm'},
            'music/Sample Mind - Freedom Trail Studio.mp3': {'cinematic','calm'},
            'music/Survival Mode - Blue Deer Studio.mp3': {'upbeat','electronic'},
            'music/Candy Apple Town - National Sweetheart.mp3': {'suspense','dark'},
            'music/Sizzr - Schwartzy.mp3': {'ambient','inspirational'},
            'music/Toys Are Us - Blue Deer Studio.mp3': {'children’s','funny'},
        }
        for file,tags in expected.items():
            self.assertIn(file,by_file)
            self.assertTrue(tags.issubset(set(by_file[file]['selection_tags'])))

    def test_checked_in_catalog_lists_the_managed_effect_collection(self):
        catalog=json.loads(Path('audio/library.json').read_text())
        by_file={entry['file']:entry for entry in catalog['sfx']}
        expected={
            'sfx/Camera Shutter.mp3':'camera shutter',
            'sfx/Clock Ticking Short.mp3':'clock ticking',
            'sfx/Discovery Sparkle.mp3':'discovery sparkle',
            'sfx/Mechanical Switch Click.mp3':'mechanical switch click',
            'sfx/Page Turn.mp3':'page turn',
            'sfx/Short Suspense Riser.mp3':'short suspense riser',
            'sfx/Soft Cinematic Boom.mp3':'soft cinematic boom',
            'sfx/Soft Discovery Chime.mp3':'soft discovery chime',
        }
        for file,purpose in expected.items():
            self.assertIn(file,by_file)
            self.assertEqual(by_file[file]['purpose'],purpose)
            self.assertTrue(by_file[file]['usage_note'])

    def test_cataloged_bucket_asset_can_calculate_hash_and_duration_at_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'sfx').mkdir()
            with wave.open(str(root/'sfx'/'Intro.wav'),'wb') as wav:
                wav.setparams((1,2,8000,0,'NONE','not compressed'))
                wav.writeframes(b'\0\0'*8000)
            path=root/'library.json';path.write_text(json.dumps({'music':[],'sfx':[{
                'id':'intro','file':'sfx/Intro.wav','attribution_required':False,'sha256':'auto'}]}))
            entry=s.load_library(path)[0]
            self.assertNotEqual(entry['sha256'],'auto')
            self.assertEqual(len(entry['sha256']),64)
            self.assertAlmostEqual(entry['duration_seconds'],1,places=2)

    def test_narration_only_is_a_valid_plan(self):
        self.assertEqual(s.resolve_plan({'music_id':None,'effects':[]},[],[],30),
                         {'music_id':None,'effects':[],'credits':[]})

    def test_required_credit_is_written_to_metadata_once(self):
        from automation.production import metadata
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'short').mkdir()
            for location in (root,root/'short'):
                (location/'storyboard.json').write_text('{"title":"Test","scenes":[]}')
                (location/'soundtrack.json').write_text('{"credits":["Music by Example, CC BY"]}')
            value={kind:{'title':'Test','description':'An explanation.','tags':['test'],'thumbnail_prompt':'Art'}
                   for kind in ('long','short')}
            value['platforms'] = {
                'facebook_long': {'title': 'Why Does This Happen?', 'description': 'A clear explanation.'},
                'facebook_reel': {'caption': 'Here is the surprising reason.', 'hashtags': ['Explained'],
                                  'cover_text': 'The Real Reason'},
                'tiktok': {'caption': 'The answer is stranger than it looks.', 'hashtags': ['LearnOnTikTok'],
                           'cover_text': 'Why It Happens'},
                'instagram': {'caption': 'A visual explanation of the answer.', 'hashtags': ['Explainer'],
                              'cover_text': 'The Hidden Answer'},
            }
            response=Mock();response.json.return_value={'status':'completed','output':[{'content':[
                {'type':'output_text','text':json.dumps(value)}]}]}
            with patch.object(p,'api_post',return_value=response) as api:
                metadata(root);metadata(root)
            instructions=api.call_args.args[3]['instructions']
            self.assertIn('simple, natural, thought-provoking questions',instructions)
            self.assertIn('Why Do Airplanes Leave White Trails in the Sky?',instructions)
            self.assertIn('3–6 topic-specific hashtags', instructions)
            schema = api.call_args.args[3]['text']['format']['schema']
            self.assertIn('platforms', schema['properties'])
            saved=json.loads((root/'metadata.json').read_text())
            self.assertEqual(saved['long']['description'].count('Music by Example, CC BY'),1)

class ReservedIntroTests(unittest.TestCase):
    def test_intro_is_forced_and_clears_competing_opening_effects(self):
        entries = [
            {'id': 'intro-id', 'kind': 'sfx', 'file': 'sfx/Intro.mp3', 'duration_seconds': 1.2,
             'clip_seconds': 1, 'start_seconds': 0, 'attribution_required': False},
            {'id': 'pop', 'kind': 'sfx', 'file': 'sfx/Pop.mp3', 'attribution_required': False},
        ]
        plan = {'music_id': None, 'effects': [
            {'effect_id': 'pop', 'at_seconds': 5, 'clip_seconds': 1, 'source_start': 0},
            {'effect_id': 'pop', 'at_seconds': 15, 'clip_seconds': 1, 'source_start': 0}], 'credits': []}
        result = s.add_reserved_intro(plan, entries, 30)
        self.assertEqual([cue['at_seconds'] for cue in result['effects']], [0, 15])
        self.assertTrue(result['effects'][0]['reserved_intro'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_intro_mix_animates_opening_and_preserves_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p.run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:s=160x90:r=10:d=2',
                          '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo:d=2',
                          '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(root/'video.mp4')])
            p.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=1200:duration=1:sample_rate=48000', str(root/'intro.wav')])
            entry = {'id': 'intro-id', 'kind': 'sfx', 'file': 'sfx/Intro.mp3', 'path': root/'intro.wav',
                     'sha256': s.file_hash(root/'intro.wav'), 'duration_seconds': 1,
                     'clip_seconds': 1, 'start_seconds': 0, 'attribution_required': False}
            plan = s.add_reserved_intro({'music_id': None, 'effects': [], 'credits': []}, [entry], 2)
            s.mix_video(root, [entry], plan)
            self.assertAlmostEqual(p.duration(root/'video.mp4'), 2, delta=.1)
            saved = json.loads((root/'soundtrack.json').read_text())
            self.assertTrue(saved['effects'][0]['reserved_intro'])
            self.assertEqual(saved['mix']['intro_effect_lufs'], -15)
            self.assertEqual(saved['mix']['intro_voice_duck'], .72)
            levels = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(root/'video.mp4'), '-vn',
                                     '-af', 'volumedetect', '-f', 'null', '-'],
                                    capture_output=True, text=True, check=True).stderr
            mean = float(__import__('re').search(r'mean_volume: ([-0-9.]+) dB', levels).group(1))
            self.assertGreater(mean, -23)
            frame = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '0', '-i', str(root/'video.mp4'),
                                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                   capture_output=True, check=True).stdout
            later = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '0.7', '-i', str(root/'video.mp4'),
                                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                   capture_output=True, check=True).stdout
            self.assertLess(sum(frame), sum(later))
