import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from automation.queue_runner import choose_item, validate_queue, process, email_summary, platform_metadata
from automation.youtube import YouTube
import pipeline as p


def item():
    return {'id': 'imax-001', 'idea': 'What is IMAX?', 'made_for_kids': False,
            'contains_synthetic_media': False}


def response(status, body=None, headers=None):
    r = Mock(status_code=status, ok=status < 400, headers=headers or {})
    r.json.return_value = body or {}
    return r


class AutomationTests(unittest.TestCase):
    def test_legacy_metadata_gets_platform_specific_fallback(self):
        value = {kind: {'title': 'Why Does It Work?', 'description': 'Explanation',
                        'tags': ['science fact', 'learning']}
                 for kind in ('long', 'short')}
        result = platform_metadata(value)
        self.assertEqual(result['facebook_long']['title'], 'Why Does It Work?')
        self.assertEqual(result['tiktok']['hashtags'], ['sciencefact', 'learning'])
        self.assertEqual(result['instagram']['cover_text'], 'Why Does It Work')

    def test_fifo_blocks_later_items_until_first_complete(self):
        q = {'items': [item(), {**item(), 'id': 'second'}]}
        self.assertEqual(choose_item(q, {'imax-001': {'status': 'awaiting_approval'}})['id'], 'imax-001')
        self.assertEqual(choose_item(q, {'imax-001': {'status': 'done'}})['id'], 'second')
        self.assertEqual(choose_item(q, {'imax-001': {'status': 'discarded'}})['id'], 'second')

    def test_queue_rejects_duplicate_and_traversal(self):
        for items in [[item(), item()], [{**item(), 'id': '../secrets'}]]:
            with self.assertRaises(ValueError):
                validate_queue({'email_mode': 'review', 'items': items})

    def test_ambiguous_upload_does_not_resubmit(self):
        yt = YouTube.__new__(YouTube); yt.session = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'video.mp4'; path.write_bytes(b'video')
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                yt.upload(path, {}, {'started': True}, Mock(), made_for_kids=False, synthetic=False)
            yt.session.post.assert_not_called()

    def test_youtube_error_exposes_safe_machine_reason_only(self):
        rejected = response(403, {'error': {'message': 'private provider text',
            'status': 'PERMISSION_DENIED', 'errors': [{'reason': 'youtubeSignupRequired'}]}})
        with self.assertRaisesRegex(RuntimeError, r'youtubeSignupRequired, PERMISSION_DENIED') as raised:
            YouTube.check(rejected)
        self.assertNotIn('private provider text', str(raised.exception))

    def test_definitive_upload_rejection_clears_started_marker(self):
        yt = YouTube.__new__(YouTube); yt.session = Mock()
        yt.session.post.return_value = response(403, {'error': {'errors': [{'reason': 'forbidden'}]}})
        state = {}; save = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'video.mp4'; path.write_bytes(b'video')
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                yt.upload(path, {}, state, save, made_for_kids=False, synthetic=False)
        self.assertNotIn('started', state); self.assertNotIn('signature', state)
        self.assertEqual(save.call_count, 2)

    def test_recommended_private_upload_defaults(self):
        yt = YouTube.__new__(YouTube); yt.session = Mock()
        yt.session.put.return_value = response(200, {})
        yt.configure_defaults('video-id', {'title': 'Title', 'description': 'Description',
            'tags': ['topic'], 'categoryId': '27'}, made_for_kids=False, synthetic=False)
        request = yt.session.put.call_args
        self.assertEqual(request.kwargs['params']['part'], 'snippet,status,paidProductPlacementDetails')
        body = request.kwargs['json']
        self.assertEqual(body['snippet']['defaultLanguage'], 'en')
        self.assertEqual(body['snippet']['defaultAudioLanguage'], 'en')
        self.assertEqual(body['snippet']['categoryId'], '27')
        self.assertEqual(body['status'], {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False,
            'containsSyntheticMedia': False, 'embeddable': True, 'license': 'youtube',
            'publicStatsViewable': True})
        self.assertEqual(body['paidProductPlacementDetails']['hasPaidProductPlacement'], False)

    def test_resumable_upload_queries_completed_job(self):
        yt = YouTube.__new__(YouTube); yt.session = Mock()
        yt.session.put.return_value = response(200, {'id': 'saved-video'})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'video.mp4'; path.write_bytes(b'video')
            state = {'session': 'https://www.googleapis.com/upload/test', 'started': True}
            self.assertEqual(yt.upload(path, {}, state, Mock(), made_for_kids=False, synthetic=False), 'saved-video')
            yt.session.post.assert_not_called()
            self.assertNotIn('session', state)

    def test_upload_checkpoints_before_post_and_resumes_chunks(self):
        yt = YouTube.__new__(YouTube); yt.session = Mock(); state = {}
        def post(*args, **kwargs):
            self.assertTrue(state['started'])
            self.assertEqual(kwargs['json']['status']['privacyStatus'], 'private')
            return response(200, headers={'Location': 'https://www.googleapis.com/upload/test'})
        yt.session.post.side_effect = post
        yt.session.put.side_effect = [response(308, headers={'Range': 'bytes=0-1'}), response(201, {'id': 'new-video'})]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'video.mp4'; path.write_bytes(b'abcdef')
            yt.upload(path, {}, state, Mock(), made_for_kids=False, synthetic=False)
        self.assertEqual(yt.session.put.call_args.kwargs['data'], b'cdef')
        self.assertEqual(state['id'], 'new-video')

    def test_empty_notes_migrates_existing_item_signature_without_regeneration(self):
        queued = {**item(), 'notes': '', 'short_seconds': 30}
        legacy = p.fingerprint({k: queued.get(k) for k in
            ('idea', 'minutes', 'made_for_kids', 'contains_synthetic_media')})
        legacy = p.fingerprint({'base': legacy, 'short_seconds': 30})
        states = {queued['id']: {'status': 'ready', 'signature': legacy}}
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.email_summary'), \
             patch('automation.queue_runner.review_digest', return_value='digest'), \
             patch('automation.queue_runner.subprocess.run') as run:
            process(queued, {'email_mode': 'review'}, Path(tmp), states, Mock())
        run.assert_not_called()
        self.assertNotEqual(states[queued['id']]['signature'], legacy)

    def test_nonempty_notes_do_not_migrate_existing_signature(self):
        queued = {**item(), 'notes': 'Use a new example.'}
        legacy = p.fingerprint({k: queued.get(k) for k in
            ('idea', 'minutes', 'made_for_kids', 'contains_synthetic_media')})
        states = {queued['id']: {'status': 'ready', 'signature': legacy}}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'settings changed'):
            process(queued, {'email_mode': 'review'}, Path(tmp), states, Mock())

    def test_review_blocks_uploads(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.email_summary') as email, \
             patch('automation.queue_runner.YouTube') as yt, patch('automation.queue_runner.review_digest', return_value='package-hash'):
            states = {'imax-001': {'status': 'ready'}}
            process(item(), {'email_mode': 'review'}, Path(tmp), states, Mock())
            self.assertEqual(states['imax-001']['status'], 'awaiting_approval')
            yt.assert_not_called(); email.assert_called_once()

    def test_uploaded_email_retry_does_not_upload_again(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.email_summary'), \
             patch('automation.queue_runner.YouTube') as yt:
            states = {'imax-001': {'status': 'uploaded'}}
            process(item(), {'email_mode': 'confirmation'}, Path(tmp), states, Mock())
            self.assertEqual(states['imax-001']['status'], 'done'); yt.assert_not_called()

    def test_unknown_email_delivery_is_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'):
            email_summary(item(), Path('/unused'), {'email_review': 'sending'}, Mock(), 'review')

    def test_all_platform_staging_precedes_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'projects/imax-001'; project.mkdir(parents=True)
            meta = {k: {'title': k, 'description': 'Description', 'tags': ['topic']} for k in ('long', 'short')}
            (project/'metadata.json').write_text(json.dumps(meta))
            events = []
            yt = Mock()
            def upload(path, snippet, upload_state, save, **kwargs):
                events.append('youtube-'+snippet['title'])
                upload_state['id'] = snippet['title']+'-id'
                return upload_state['id']
            yt.upload.side_effect = upload
            fb = Mock()
            def facebook_long(path, metadata, upload_state, save):
                events.append('facebook-long'); upload_state['id'] = 'fb-long'
            def facebook_short(path, metadata, upload_state, save):
                events.append('facebook-short'); upload_state.update(id='fb-short', finished=True)
            fb.upload_unpublished_video.side_effect = facebook_long
            fb.upload_reel_draft.side_effect = facebook_short
            tik = Mock()
            def tiktok_short(path, upload_state, save):
                events.append('tiktok-short'); upload_state.update(publish_id='tt-short', uploaded=True)
            tik.upload_draft.side_effect = tiktok_short
            states = {'imax-001': {'status': 'ready'}}
            with patch('automation.queue_runner.YouTube', return_value=yt), \
                 patch('automation.queue_runner.Meta', return_value=fb), \
                 patch('automation.queue_runner.TikTok', return_value=tik), \
                 patch('automation.queue_runner.email_summary', side_effect=lambda *args: events.append('email')):
                process(item(), {'email_mode': 'confirmation'}, root, states, Mock())
            self.assertEqual(events, ['youtube-long', 'youtube-short', 'facebook-long',
                                      'facebook-short', 'tiktok-short', 'email'])
            self.assertEqual(yt.configure_defaults.call_count, 2)
            self.assertTrue(states['imax-001']['uploads']['long']['defaults_set'])
            self.assertTrue(states['imax-001']['uploads']['short']['defaults_set'])
            self.assertEqual(states['imax-001']['status'], 'done')
            self.assertIn('tiktok', states['_platform_credentials'])

    def test_thumbnail_rejection_does_not_block_other_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'projects/imax-001'; project.mkdir(parents=True)
            (project/'metadata.json').write_text(json.dumps({
                k: {'title': k, 'description': 'Description', 'tags': ['topic']} for k in ('long', 'short')}))
            yt = Mock(); events = []
            def upload(path, snippet, upload_state, save, **kwargs):
                events.append('upload-'+snippet['title']); upload_state['id'] = snippet['title']+'-id'
                return upload_state['id']
            yt.upload.side_effect = upload
            yt.thumbnail.side_effect = RuntimeError('HTTP 403 (forbidden)')
            fb = Mock()
            fb.upload_unpublished_video.side_effect = lambda path, metadata, state, save: state.update(id='fb-long')
            fb.upload_reel_draft.side_effect = lambda path, metadata, state, save: state.update(id='fb-short', finished=True)
            tik = Mock()
            tik.upload_draft.side_effect = lambda path, state, save: state.update(publish_id='tt-short', uploaded=True)
            states = {'imax-001': {'status': 'ready'}}
            with patch('automation.queue_runner.YouTube', return_value=yt), \
                 patch('automation.queue_runner.Meta', return_value=fb), \
                 patch('automation.queue_runner.TikTok', return_value=tik), \
                 patch('automation.queue_runner.email_summary'):
                process(item(), {'email_mode': 'confirmation'}, root, states, Mock())
            self.assertEqual(events, ['upload-long', 'upload-short'])
            self.assertEqual(states['imax-001']['status'], 'done')
            self.assertIn('HTTP 403', states['imax-001']['thumbnail_error'])

    def test_wrong_approval_cannot_publish(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.review_digest', return_value='current'), \
             patch('automation.queue_runner.email_summary'), patch('automation.queue_runner.YouTube') as yt, \
             patch.dict('os.environ', {'APPROVAL_CODE': 'stale'}):
            states = {'imax-001': {'status': 'ready', 'review_digest': 'current', 'email_review': 'sent'}}
            with self.assertRaisesRegex(ValueError, 'Approval does not match'):
                process(item(), {'email_mode': 'review'}, Path(tmp), states, Mock())
            yt.assert_not_called()

    def test_changed_package_revokes_approval(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.review_digest', return_value='new'), \
             patch('automation.queue_runner.email_summary'), patch('automation.queue_runner.YouTube') as yt, \
             patch.dict('os.environ', {'APPROVAL_CODE': ''}):
            states = {'imax-001': {'status': 'ready', 'review_digest': 'old', 'approved_digest': 'old', 'email_review': 'sent'}}
            process(item(), {'email_mode': 'review'}, Path(tmp), states, Mock())
            self.assertNotIn('approved_digest', states['imax-001'])
            self.assertEqual(states['imax-001']['status'], 'awaiting_approval')
            yt.assert_not_called()

    def test_delete_discards_cloud_package_without_uploading(self):
        state = {'status': 'awaiting_approval', 'review_digest': 'v1', 'email_review': 'sent',
                 'revision': 2}
        states = {'imax-001': state}; cloud = Mock(); save = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                'os.environ', {'REVIEW_ACTION': 'delete', 'APPROVAL_CODE': 'v1'}), \
                patch('automation.queue_runner.subprocess.run') as run, \
                patch('automation.queue_runner.YouTube') as youtube:
            process(item(), {'email_mode': 'review'}, Path(tmp), states, save, cloud)
        cloud.discard_project.assert_called_once_with('imax-001')
        run.assert_not_called(); youtube.assert_not_called()
        self.assertEqual(state['status'], 'discarded')
        self.assertEqual(state['revision'], 2)
        self.assertNotIn('review_digest', state)
        self.assertGreaterEqual(save.call_count, 2)

    def test_delete_rejects_missing_stale_or_uploaded_package(self):
        from automation.queue_runner import apply_deletion
        base = {'status': 'awaiting_approval', 'review_digest': 'right', 'email_review': 'sent'}
        cases = [
            ({**base}, {'REVIEW_ACTION': 'delete', 'APPROVAL_CODE': ''}),
            ({**base}, {'REVIEW_ACTION': 'delete', 'APPROVAL_CODE': 'wrong'}),
            ({**base, 'uploads': {'long': {'id': 'youtube-id'}}},
             {'REVIEW_ACTION': 'delete', 'APPROVAL_CODE': 'right'}),
        ]
        for state, environment in cases:
            with self.subTest(state=state), patch.dict('os.environ', environment):
                cloud = Mock()
                with self.assertRaises(ValueError):
                    apply_deletion(item(), state, Path('/unused'), Mock(), cloud)
                cloud.discard_project.assert_not_called()

    def test_regeneration_archives_and_resets_approval_only_once(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'v1', 'email_review': 'sent',
                 'approved_digest': 'v1', 'signature': 'idea'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate', 'APPROVAL_CODE': 'v1'}):
            save = Mock()
            apply_regeneration(item(), state, save)
            self.assertEqual(state['revision'], 2)
            self.assertNotIn('approved_digest', state)
            self.assertNotIn('email_review', state)
            self.assertEqual(state['history'][0]['review_digest'], 'v1')
            apply_regeneration(item(), state, save)
            self.assertEqual(state['revision'], 2)
            self.assertEqual(save.call_count, 1)

    def test_regeneration_stores_optional_comments_for_the_new_revision(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'v1', 'email_review': 'sent',
                 'signature': 'idea'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate', 'APPROVAL_CODE': 'v1',
                                       'REVIEW_COMMENTS': 'Use a calmer opening and fewer close-ups.'}):
            apply_regeneration(item(), state, Mock())
        self.assertEqual(state['regeneration_comments'], 'Use a calmer opening and fewer close-ups.')
        self.assertEqual(state['revision'], 2)

    def test_thumbnail_regeneration_changes_only_thumbnail_and_revokes_old_code(self):
        from automation.queue_runner import apply_thumbnail_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp); (project/'thumbnail.jpg').write_bytes(b'old thumbnail')
            (project/'metadata.json').write_text(json.dumps({'long': {'title': 'Why IMAX?',
                'thumbnail_prompt': 'A giant screen'}}))
            state = {'status': 'awaiting_approval', 'review_digest': 'v1-full-code',
                     'email_review': 'sent', 'approved_digest': 'v1-full-code'}
            save = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_thumbnail',
                    'APPROVAL_CODE': 'v1-full-code', 'REVIEW_COMMENTS': 'Show the screen much larger.'}), \
                 patch('automation.queue_runner.thumbnail') as generate:
                apply_thumbnail_regeneration(item(), state, project, save)
                apply_thumbnail_regeneration(item(), state, project, save)
            generate.assert_called_once()
            self.assertEqual(generate.call_args.kwargs['comments'], 'Show the screen much larger.')
            self.assertTrue((project/'thumbnail-history/thumbnail-v1-full-code.jpg').exists())
            self.assertEqual(state['thumbnail_revision'], 2)
            self.assertNotIn('review_digest', state)
            self.assertNotIn('approved_digest', state)
            self.assertEqual(save.call_count, 1)

    def test_consumed_thumbnail_code_rejects_different_comments(self):
        from automation.queue_runner import apply_thumbnail_regeneration
        state = {'status': 'awaiting_approval', 'thumbnail_regeneration_requests': ['used-code'],
                 'thumbnail_comments': 'First direction'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_thumbnail',
                'APPROVAL_CODE': 'used-code', 'REVIEW_COMMENTS': 'Different direction'}):
            with self.assertRaisesRegex(ValueError, 'already generated'):
                apply_thumbnail_regeneration(item(), state, Path('/unused'), Mock())

    def test_thumbnail_regeneration_rejects_stale_code_or_started_upload(self):
        from automation.queue_runner import apply_thumbnail_regeneration
        base = {'status': 'awaiting_approval', 'review_digest': 'right', 'email_review': 'sent'}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp); (project/'metadata.json').write_text(json.dumps({'long': {}}))
            for state, code in [({**base}, 'wrong'),
                                ({**base, 'uploads': {'long': {'id': 'uploaded'}}}, 'right')]:
                with self.subTest(state=state), patch.dict('os.environ', {
                        'REVIEW_ACTION': 'regenerate_thumbnail', 'APPROVAL_CODE': code}):
                    with self.assertRaises(ValueError):
                        apply_thumbnail_regeneration(item(), state, project, Mock())

    def test_regeneration_rejects_stale_code_and_uploaded_video(self):
        from automation.queue_runner import apply_regeneration
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate', 'APPROVAL_CODE': 'old'}):
            for state in [{'status': 'awaiting_approval', 'review_digest': 'new', 'email_review': 'sent'},
                          {'status': 'publishing', 'review_digest': 'old', 'uploads': {'long': {'id': 'uploaded'}}}]:
                with self.assertRaises(ValueError):
                    apply_regeneration(item(), state, Mock())

    def test_queue_accepts_thirty_second_short_and_rejects_invalid_targets(self):
        validate_queue({'email_mode':'review', 'items':[{**item(), 'short_seconds':30}]})
        for seconds in (29, 61, 30.5, True):
            with self.assertRaises(ValueError):
                validate_queue({'email_mode':'review', 'items':[{**item(), 'short_seconds':seconds}]})

    def test_queue_accepts_optional_text_notes(self):
        validate_queue({'email_mode':'review', 'items':[{**item(), 'notes':'Focus on projector technology.'}]})
        with self.assertRaisesRegex(ValueError, 'notes must be text'):
            validate_queue({'email_mode':'review', 'items':[{**item(), 'notes':['projectors']}]})

    def test_review_only_blocks_youtube_even_with_matching_approval(self):
        from automation import queue_runner as q
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            states = {item()['id']:{'status':'ready','review_digest':'digest','email_review':'sent'}}
            with patch.dict('os.environ', {'REVIEW_ONLY':'1', 'APPROVAL_CODE':'digest'}), \
                 patch.object(q, 'review_digest', return_value='digest'), \
                 patch.object(q, 'email_summary'), patch.object(q, 'YouTube') as youtube:
                process(item(), {'email_mode':'confirmation'}, root, states, Mock())
            youtube.assert_not_called()
            self.assertEqual(states[item()['id']]['status'], 'awaiting_approval')
