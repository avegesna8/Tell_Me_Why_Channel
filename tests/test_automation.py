import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
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
            def facebook_long(path, metadata, upload_state, save, **kwargs):
                events.append('facebook-long'); upload_state['id'] = 'fb-long'
            def facebook_short(path, metadata, upload_state, save):
                events.append('facebook-short'); upload_state.update(id='fb-short', finished=True)
            fb.upload_unpublished_video.side_effect = facebook_long
            fb.upload_reel_draft.side_effect = facebook_short
            def instagram_short(path, metadata, upload_state, save):
                events.append('instagram-short'); upload_state['id'] = 'ig-short'
            fb.publish_instagram_reel.side_effect = instagram_short
            states = {'imax-001': {'status': 'ready'}}
            with patch('automation.queue_runner.YouTube', return_value=yt), \
                 patch('automation.queue_runner.Meta', return_value=fb), \
                 patch('automation.queue_runner.email_summary', side_effect=lambda *args: events.append('email')):
                process(item(), {'email_mode': 'confirmation'}, root, states, Mock())
            self.assertEqual(events, ['youtube-long', 'youtube-short', 'facebook-long',
                                      'facebook-short', 'instagram-short', 'email'])
            self.assertEqual(yt.configure_defaults.call_count, 2)
            self.assertTrue(states['imax-001']['uploads']['long']['defaults_set'])
            self.assertTrue(states['imax-001']['uploads']['short']['defaults_set'])
            self.assertEqual(states['imax-001']['status'], 'done')
            self.assertEqual(states['imax-001']['platform_uploads']['tiktok']['short']['delivery_status'],
                             'manual_upload_required')
            self.assertNotIn('_platform_credentials', states)

    def test_tiktok_is_always_left_for_manual_studio_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'projects/imax-001'; project.mkdir(parents=True)
            (project/'metadata.json').write_text(json.dumps({
                k: {'title': k, 'description': 'Description', 'tags': ['topic']} for k in ('long', 'short')}))
            states = {'imax-001': {'status': 'publishing', 'uploads': {
                'long': {'id': 'yt-long'}, 'short': {'id': 'yt-short'}},
                'platform_uploads': {'facebook': {
                    'long': {'id': 'fb-long'}, 'short': {'id': 'fb-short', 'finished': True}},
                    'instagram': {'short': {'id': 'ig-short'}},
                    'tiktok': {'short': {'started': True, 'signature': 'safe-pre-upload'}}}}}
            with patch('automation.queue_runner.email_summary'):
                process(item(), {'email_mode': 'confirmation'}, root, states, Mock())
            short = states['imax-001']['platform_uploads']['tiktok']['short']
            self.assertTrue(short['skipped'])
            self.assertEqual(short['delivery_status'], 'manual_upload_required')
            self.assertNotIn('started', short)
            self.assertNotIn('signature', short)
            self.assertEqual(states['imax-001']['status'], 'done')

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
            fb.upload_unpublished_video.side_effect = lambda path, metadata, state, save, **kwargs: state.update(id='fb-long')
            fb.upload_reel_draft.side_effect = lambda path, metadata, state, save: state.update(id='fb-short', finished=True)
            fb.publish_instagram_reel.side_effect = lambda path, metadata, state, save: state.update(id='ig-short')
            states = {'imax-001': {'status': 'ready'}}
            with patch('automation.queue_runner.YouTube', return_value=yt), \
                 patch('automation.queue_runner.Meta', return_value=fb), \
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

    def test_restart_deletes_only_current_project_and_clears_state(self):
        from automation.queue_runner import apply_restart
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'projects' / 'imax-001'
            project.mkdir(parents=True)
            (project / 'video.mp4').write_bytes(b'old')
            shared = root / 'shared.txt'
            shared.write_text('keep')
            state = {'status': 'generating', 'review_digest': 'old', 'uploads': {'long': {'id': 'old'}}}
            save = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'restart',
                                          'EXISTING_UPLOADS_REMOVED': '1'}, clear=True):
                self.assertTrue(apply_restart(item(), state, root, save))
            self.assertEqual(state, {})
            self.assertFalse(project.exists())
            self.assertEqual(shared.read_text(), 'keep')
            save.assert_called_once()

    def test_restart_cloud_deletes_only_current_project(self):
        from automation.queue_runner import apply_restart
        state = {'status': 'generating'}
        cloud = Mock()
        with patch.dict('os.environ', {'REVIEW_ACTION': 'restart',
                                      'EXISTING_UPLOADS_REMOVED': '1'}, clear=True):
            self.assertTrue(apply_restart(item(), state, Path('/unused'), Mock(), cloud))
        cloud.discard_project.assert_called_once_with('imax-001')
        self.assertEqual(state, {})

    def test_restart_requires_platform_cleanup_confirmation(self):
        from automation.queue_runner import apply_restart
        with patch.dict('os.environ', {'REVIEW_ACTION': 'restart'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'confirming'):
                apply_restart(item(), {'status': 'ready'}, Path('/unused'), Mock())

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

    def test_selected_regeneration_removes_only_requested_output(self):
        from automation.queue_runner import apply_selected_regeneration
        for kind in ('long', 'short'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                long_files = [project/'video.mp4', project/'script.txt', project/'storyboard.json',
                              project/'thumbnail.jpg', project/'assets/scene-001.png']
                short_files = [project/'short/video.mp4', project/'short/script.txt',
                               project/'short/storyboard.json', project/'short/assets/scene-001.png']
                shared_files = [project/'metadata.json', project/'metadata-source.json']
                for path in long_files + short_files + shared_files:
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
                state = {'status': 'awaiting_approval', 'review_digest': 'code',
                         'email_review': 'sent', 'approved_digest': 'code'}
                cloud = Mock(); save = Mock()
                with patch.dict('os.environ', {'REVIEW_ACTION': f'regenerate_{kind}',
                        'APPROVAL_CODE': 'code', 'REVIEW_COMMENTS': f'Improve {kind} pacing.'}):
                    apply_selected_regeneration(item(), state, project, save, cloud)
                preserved = short_files if kind == 'long' else long_files
                removed = long_files if kind == 'long' else short_files
                self.assertTrue(all(path.exists() for path in preserved + shared_files))
                self.assertTrue(all(not path.exists() for path in removed))
                self.assertEqual(state['selected_regeneration_active'], kind)
                self.assertEqual(state[f'{kind}_regeneration_comments'], f'Improve {kind} pacing.')
                self.assertNotIn('review_digest', state)
                cloud.discard_output.assert_called_once_with(project, kind)
                self.assertEqual(save.call_count, 2)

    def test_long_regeneration_does_not_rewrite_short_covers(self):
        import automation.queue_runner as runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root/'projects'/'imax-001'
            (project/'short/covers').mkdir(parents=True)
            for path in (project/'video.mp4', project/'short/video.mp4', project/'metadata.json',
                         project/'thumbnail.jpg', project/'script.txt', project/'short/script.txt',
                         project/'script-quality.json'):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'data')
            (project/'metadata.json').write_text(json.dumps({'long': {'title': 'Long'}}))
            state = {'status': 'generating', 'signature': p.fingerprint({
                         k: item().get(k) for k in ('idea', 'notes', 'minutes', 'made_for_kids',
                                                   'contains_synthetic_media')}),
                     'long_regeneration_requests': ['used'], 'long_regeneration_comments': '',
                     'selected_regeneration_active': 'long',
                     'long_regeneration_cleanup_done': True}
            states = {'imax-001': state}
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_long',
                                          'APPROVAL_CODE': 'used', 'REVIEW_COMMENTS': '',
                                          'REVIEW_ONLY': '1'}, clear=True),                  patch('automation.queue_runner.subprocess.run'),                  patch('automation.queue_runner.metadata'),                  patch('automation.queue_runner.thumbnail'),                  patch('automation.queue_runner.social_covers') as covers,                  patch('automation.queue_runner.review_digest', return_value='new'),                  patch('automation.queue_runner.email_summary'):
                runner.process(item(), {'email_mode': 'review'}, root, states, Mock())
            covers.assert_not_called()
            self.assertEqual(state['status'], 'awaiting_approval')

    def test_selected_regeneration_strips_copied_approval_code_whitespace(self):
        from automation.queue_runner import apply_selected_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for path in (project/'video.mp4', project/'script.txt', project/'storyboard.json', project/'thumbnail.jpg'):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
            state = {'status': 'awaiting_approval', 'review_digest': 'new-code', 'email_review': 'sent'}
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_short',
                                          'APPROVAL_CODE': '  new-code \n', 'REVIEW_COMMENTS': ''}):
                apply_selected_regeneration(item(), state, project, Mock())
            self.assertEqual(state['short_regeneration_requests'], ['new-code'])

    def test_selected_regeneration_reconciles_received_email_with_current_code(self):
        from automation.queue_runner import apply_selected_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for path in (project/'video.mp4', project/'script.txt', project/'storyboard.json', project/'thumbnail.jpg'):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
            state = {'status': 'awaiting_approval', 'review_digest': 'current-code'}
            save = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_short',
                                          'APPROVAL_CODE': 'current-code', 'REVIEW_COMMENTS': ''}):
                apply_selected_regeneration(item(), state, project, save)
            self.assertEqual(state['short_regeneration_requests'], ['current-code'])
            self.assertTrue(state['email_review_reconciled'])
            self.assertEqual(save.call_count, 3)

    def test_selected_regeneration_does_not_reconcile_stale_code(self):
        from automation.queue_runner import apply_selected_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'current-code'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_short',
                                      'APPROVAL_CODE': 'stale-code', 'REVIEW_COMMENTS': ''}):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                apply_selected_regeneration(item(), state, Path('/unused'), Mock())
        self.assertNotIn('email_review', state)
        self.assertNotIn('email_review_reconciled', state)

    def test_selected_regeneration_reuses_consumed_code_and_resumes_cleanup(self):
        from automation.queue_runner import apply_selected_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp); (project/'short').mkdir(); (project/'short/video.mp4').write_bytes(b'partial')
            state = {'status': 'generating', 'short_regeneration_requests': ['used'],
                     'short_regeneration_comments': 'Try a stronger hook.',
                     'selected_regeneration_active': 'short'}
            cloud = Mock(); save = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_short', 'APPROVAL_CODE': 'used',
                                          'REVIEW_COMMENTS': 'Try a stronger hook.'}):
                apply_selected_regeneration(item(), state, project, save, cloud)
            self.assertTrue((project/'short/video.mp4').exists())
            cloud.discard_output.assert_not_called()
            self.assertTrue(state['short_regeneration_cleanup_done'])
            save.assert_called_once()

    def test_selected_audio_regeneration_preserves_other_video_and_all_visuals(self):
        from automation.queue_runner import apply_selected_audio_regeneration
        for kind in ('long', 'short'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                keep = [project/'script.txt', project/'storyboard.json', project/'assets/scene-001.png',
                        project/'short/script.txt', project/'short/storyboard.json',
                        project/'short/assets/scene-001.png', project/'thumbnail.jpg']
                long_audio = [project/'video.mp4', project/'assets/narration.wav', project/'soundtrack.json']
                short_audio = [project/'short/video.mp4', project/'short/assets/narration.wav',
                               project/'short/soundtrack.json', project/'short/short-timing.json']
                for path in keep + long_audio + short_audio:
                    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'data')
                state = {'status': 'awaiting_approval', 'review_digest': 'code',
                         'email_review': 'sent', 'approved_digest': 'code'}
                save = Mock(); cloud = Mock()
                with patch.dict('os.environ', {'REVIEW_ACTION': f'regenerate_{kind}_audio',
                        'APPROVAL_CODE': 'code', 'REVIEW_COMMENTS': f'Fix {kind} delivery.'}):
                    apply_selected_audio_regeneration(item(), state, project, save, cloud)
                removed = long_audio if kind == 'long' else short_audio
                preserved_audio = short_audio if kind == 'long' else long_audio
                self.assertTrue(all(not path.exists() for path in removed))
                self.assertTrue(all(path.exists() for path in keep + preserved_audio))
                self.assertEqual(state['selected_audio_regeneration_active'], kind)
                self.assertTrue(state['audio_regeneration_active'])
                self.assertEqual(state[f'{kind}_audio_regeneration_comments'], f'Fix {kind} delivery.')
                self.assertNotIn('review_digest', state)
                self.assertEqual(save.call_count, 2)
                cloud.discard_audio_output.assert_called_once_with(project, kind)

    def test_selected_audio_regeneration_reconciles_received_email_with_current_code(self):
        from automation.queue_runner import apply_selected_audio_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for path in (project/'script.txt', project/'storyboard.json', project/'assets/scene-001.png'):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
            state = {'status': 'awaiting_approval', 'review_digest': 'current-code'}
            save = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_long_audio',
                                          'APPROVAL_CODE': 'current-code', 'REVIEW_COMMENTS': ''}):
                apply_selected_audio_regeneration(item(), state, project, save)
            self.assertEqual(state['long_audio_regeneration_requests'], ['current-code'])
            self.assertTrue(state['email_review_reconciled'])
            self.assertEqual(save.call_count, 3)

    def test_selected_audio_regeneration_does_not_reconcile_stale_code(self):
        from automation.queue_runner import apply_selected_audio_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'current-code'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_long_audio',
                                      'APPROVAL_CODE': 'stale-code', 'REVIEW_COMMENTS': ''}):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                apply_selected_audio_regeneration(item(), state, Path('/unused'), Mock())
        self.assertNotIn('email_review', state)
        self.assertNotIn('email_review_reconciled', state)

    def test_selected_audio_retry_resumes_only_its_cleanup(self):
        from automation.queue_runner import apply_selected_audio_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for path in (project/'video.mp4', project/'short/video.mp4'):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'video')
            state = {'status': 'generating', 'long_audio_regeneration_requests': ['used'],
                     'long_audio_regeneration_comments': '', 'selected_audio_regeneration_active': 'long'}
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_long_audio',
                                          'APPROVAL_CODE': 'used', 'REVIEW_COMMENTS': ''}):
                save = Mock(); apply_selected_audio_regeneration(item(), state, project, save)
            self.assertTrue((project/'video.mp4').exists())
            self.assertTrue((project/'short/video.mp4').exists())
            self.assertTrue(state['long_audio_regeneration_cleanup_done'])
            save.assert_called_once()

    def test_audio_regeneration_preserves_visuals_and_removes_only_audio_outputs(self):
        from automation.queue_runner import apply_audio_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for path in (project/'script.txt', project/'storyboard.json', project/'metadata.json',
                         project/'thumbnail.jpg', project/'short/script.txt', project/'short/storyboard.json',
                         project/'assets/scene-001.png', project/'short/assets/scene-001.png'):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
            removed = [project/'video.mp4', project/'video.narration.mp4', project/'soundtrack.json',
                       project/'assets/narration.wav', project/'assets/narration.audio.json',
                       project/'short/video.mp4', project/'short/assets/narration-part-001.wav',
                       project/'short/short-timing.json']
            for path in removed:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'remove')
            state = {'status': 'awaiting_approval', 'review_digest': 'code',
                     'email_review': 'sent', 'approved_digest': 'code'}
            cloud = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_audio',
                    'APPROVAL_CODE': 'code', 'REVIEW_COMMENTS': 'Fix pacing.'}):
                apply_audio_regeneration(item(), state, project, Mock(), cloud)
            self.assertTrue((project/'assets/scene-001.png').exists())
            self.assertTrue((project/'short/assets/scene-001.png').exists())
            self.assertTrue((project/'thumbnail.jpg').exists())
            self.assertTrue(all(not path.exists() for path in removed))
            self.assertEqual(state['audio_revision'], 2)
            self.assertTrue(state['audio_regeneration_active'])
            self.assertEqual(state['status'], 'generating')
            self.assertTrue(state['audio_regeneration_cleanup_done'])
            self.assertNotIn('review_digest', state)
            self.assertEqual(
                cloud.discard_audio_output.call_args_list,
                [call(project, 'long'), call(project, 'short')],
            )

    def test_audio_regeneration_retry_resumes_cloud_and_local_cleanup(self):
        from automation.queue_runner import apply_audio_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project/'video.mp4').write_bytes(b'partial')
            (project/'short').mkdir()
            (project/'short/video.mp4').write_bytes(b'partial')
            state = {'status': 'generating', 'audio_regeneration_requests': ['used'],
                     'audio_regeneration_comments': '', 'audio_regeneration_active': True,
                     'audio_regeneration_cleanup_done': False}
            cloud = Mock(); save = Mock()
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_audio',
                                          'APPROVAL_CODE': 'used', 'REVIEW_COMMENTS': ''}, clear=True):
                apply_audio_regeneration(item(), state, project, save, cloud)
            self.assertFalse((project/'video.mp4').exists())
            self.assertFalse((project/'short/video.mp4').exists())
            self.assertTrue(state['audio_regeneration_cleanup_done'])
            self.assertEqual(
                cloud.discard_audio_output.call_args_list,
                [call(project, 'long'), call(project, 'short')],
            )
            save.assert_called_once()

    def test_audio_regeneration_reconciles_current_received_email(self):
        from automation.queue_runner import apply_audio_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for path in (project/'script.txt', project/'storyboard.json', project/'metadata.json',
                         project/'thumbnail.jpg', project/'short/script.txt', project/'short/storyboard.json'):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'keep')
            state = {'status': 'awaiting_approval', 'review_digest': 'current'}
            with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_audio',
                                          'APPROVAL_CODE': 'current', 'REVIEW_COMMENTS': ''}, clear=True):
                apply_audio_regeneration(item(), state, project, Mock())
            self.assertTrue(state['email_review_reconciled'])

    def test_audio_regeneration_rejects_stale_code_or_started_upload(self):
        from automation.queue_runner import apply_audio_regeneration
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate_audio', 'APPROVAL_CODE': 'old'}):
            for state in [{'status': 'awaiting_approval', 'review_digest': 'new', 'email_review': 'sent'},
                          {'status': 'publishing', 'review_digest': 'old', 'uploads': {'long': {'id': 'uploaded'}}}]:
                with self.subTest(state=state), self.assertRaises(ValueError):
                    apply_audio_regeneration(item(), state, Path('/unused'), Mock())

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
            self.assertEqual(save.call_count, 2)

    def test_thumbnail_regeneration_resumes_after_generator_failure(self):
        from automation.queue_runner import apply_thumbnail_regeneration
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project/'thumbnail.jpg').write_bytes(b'original')
            (project/'metadata.json').write_text(json.dumps({'long': {
                'title': 'Why IMAX?', 'thumbnail_prompt': 'A giant screen'}}))
            state = {'status': 'awaiting_approval', 'review_digest': 'code', 'email_review': 'sent'}
            save = Mock()
            env = {'REVIEW_ACTION': 'regenerate_thumbnail', 'APPROVAL_CODE': 'code',
                   'REVIEW_COMMENTS': 'Make it clearer.'}
            with patch.dict('os.environ', env, clear=True), \
                 patch('automation.queue_runner.thumbnail', side_effect=RuntimeError('temporary')):
                with self.assertRaisesRegex(RuntimeError, 'temporary'):
                    apply_thumbnail_regeneration(item(), state, project, save)
            self.assertTrue(state['thumbnail_regeneration_active'])
            self.assertEqual((project/'thumbnail-history/thumbnail-code.jpg').read_bytes(), b'original')
            (project/'thumbnail.jpg').write_bytes(b'partial-new')
            with patch.dict('os.environ', env, clear=True), \
                 patch('automation.queue_runner.thumbnail') as generate:
                apply_thumbnail_regeneration(item(), state, project, save)
            generate.assert_called_once()
            self.assertEqual((project/'thumbnail-history/thumbnail-code.jpg').read_bytes(), b'original')
            self.assertNotIn('thumbnail_regeneration_active', state)
            self.assertEqual(state['thumbnail_revision'], 2)

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

    def test_regeneration_ignores_manual_tiktok_and_empty_platform_placeholders(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'current-code',
                 'platform_uploads': {
                     'tiktok': {'short': {'skipped': True, 'delivery_status': 'manual_upload_required'}},
                     'facebook': {'long': {}, 'short': {}}, 'instagram': {'short': {}}}}
        save = Mock()
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate',
                                      'APPROVAL_CODE': 'current-code', 'REVIEW_COMMENTS': ''}):
            apply_regeneration(item(), state, save)
        self.assertEqual(state['revision'], 2)
        self.assertTrue(state['history'][0]['email_review_reconciled'])
        self.assertEqual(save.call_count, 2)

    def test_upload_activity_detection_ignores_markers_but_detects_real_starts(self):
        from automation.queue_runner import upload_activity_paths, uploads_started
        self.assertFalse(uploads_started({'platform_uploads': {
            'tiktok': {'short': {'skipped': True, 'delivery_status': 'manual_upload_required'}},
            'facebook': {'long': {}}, 'instagram': {'short': {}}}}))
        for field, value in [('started', True), ('id', 'external-id'),
                             ('container_id', 'container'), ('upload_started', True)]:
            with self.subTest(field=field):
                self.assertTrue(uploads_started({'platform_uploads': {
                    'facebook': {'long': {field: value}}}}))
        self.assertEqual(
            upload_activity_paths({'uploads': {'long': {'id': 'youtube-id'}}}),
            ['youtube.long (id)'],
        )

    def test_full_regeneration_accepts_confirmed_removed_platform_uploads(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'uploaded', 'review_digest': 'current-code', 'email_review': 'sent',
                 'uploads': {'long': {'id': 'deleted-youtube-id'}},
                 'platform_uploads': {'facebook': {'long': {'id': 'deleted-facebook-id'}}}}
        save = Mock()
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate',
                                      'APPROVAL_CODE': 'current-code', 'REVIEW_COMMENTS': '',
                                      'EXISTING_UPLOADS_REMOVED': '1'}):
            apply_regeneration(item(), state, save)
        self.assertEqual(state['revision'], 2)
        self.assertNotIn('uploads', state)
        self.assertNotIn('platform_uploads', state)
        self.assertEqual(state['history'][0]['uploads']['long']['id'], 'deleted-youtube-id')

    def test_full_regeneration_recovers_interrupted_generating_status(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'generating', 'review_digest': 'current-code', 'email_review': 'sent'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate',
                                      'APPROVAL_CODE': 'current-code', 'REVIEW_COMMENTS': ''}, clear=True):
            apply_regeneration(item(), state, Mock())
        self.assertEqual(state['revision'], 2)
        self.assertEqual(state['status'], 'generating')
        self.assertEqual(state['history'][0]['status'], 'generating')

    def test_full_regeneration_reports_existing_upload_checkpoints(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'current-code',
                 'email_review': 'sent', 'uploads': {'long': {'id': 'youtube-id'}}}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate',
                                      'APPROVAL_CODE': 'current-code', 'REVIEW_COMMENTS': ''}, clear=True):
            with self.assertRaisesRegex(ValueError, r'upload checkpoints: youtube\.long \(id\).*confirmed no previous'):
                apply_regeneration(item(), state, Mock())

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
