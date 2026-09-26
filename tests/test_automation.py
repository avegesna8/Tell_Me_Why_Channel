import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta
from automation.queue_runner import approval_schedule, choose_item, validate_queue, process, email_summary
from automation.youtube import YouTube, future_time
import pipeline as p


def item():
    return {'id': 'imax-001', 'idea': 'What is IMAX?', 'made_for_kids': False,
            'contains_synthetic_media': False, 'publish_at': '2099-01-01T12:00:00-05:00',
            'short_publish_at': '2099-01-02T12:00:00-05:00'}


def response(status, body=None, headers=None):
    r = Mock(status_code=status, ok=status < 400, headers=headers or {})
    r.json.return_value = body or {}
    return r


class AutomationTests(unittest.TestCase):
    def test_fifo_blocks_later_items_until_first_complete(self):
        q = {'items': [item(), {**item(), 'id': 'second'}]}
        self.assertEqual(choose_item(q, {'imax-001': {'status': 'awaiting_approval'}})['id'], 'imax-001')
        self.assertEqual(choose_item(q, {'imax-001': {'status': 'done'}})['id'], 'second')

    def test_queue_rejects_duplicate_and_traversal(self):
        for items in [[item(), item()], [{**item(), 'id': '../secrets'}]]:
            with self.assertRaises(ValueError):
                validate_queue({'email_mode': 'review', 'items': items})

    def test_approval_schedule_overrides_queue_times_independently(self):
        queued = item()
        with patch.dict('os.environ', {'APPROVAL_PUBLISH_AT': '2099-02-01T09:30:00-05:00',
                                       'APPROVAL_SHORT_PUBLISH_AT': ''}, clear=False):
            self.assertEqual(approval_schedule(queued), {
                'publish_at': '2099-02-01T09:30:00-05:00',
                'short_publish_at': queued['short_publish_at']})

    def test_approval_schedule_requires_a_time_from_form_or_queue(self):
        queued = item(); queued.pop('publish_at')
        with patch.dict('os.environ', {'APPROVAL_PUBLISH_AT': '', 'APPROVAL_SHORT_PUBLISH_AT': ''}, clear=False):
            with self.assertRaisesRegex(ValueError, 'publish_at is required'):
                approval_schedule(queued)

    def test_schedule_requires_future_aware_time(self):
        for value in ['2020-01-01T00:00:00Z', '2099-01-01T12:00:00']:
            with self.assertRaises(ValueError): future_time(value)
        self.assertEqual(future_time('2099-01-01T12:00:00-05:00'), '2099-01-01T17:00:00Z')

    def test_ambiguous_upload_does_not_resubmit(self):
        yt = YouTube.__new__(YouTube); yt.session = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'video.mp4'; path.write_bytes(b'video')
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                yt.upload(path, {}, {'started': True}, Mock(), made_for_kids=False, synthetic=False)
            yt.session.post.assert_not_called()

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

    def test_scheduled_email_retry_does_not_upload_again(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.email_summary'), \
             patch('automation.queue_runner.YouTube') as yt:
            states = {'imax-001': {'status': 'scheduled'}}
            process(item(), {'email_mode': 'confirmation'}, Path(tmp), states, Mock())
            self.assertEqual(states['imax-001']['status'], 'done'); yt.assert_not_called()

    def test_unknown_email_delivery_is_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'):
            email_summary(item(), Path('/unused'), {'email_review': 'sending'}, Mock(), 'review')

    def test_both_private_uploads_precede_scheduling_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root/'projects/imax-001'; project.mkdir(parents=True)
            meta = {k: {'title': k, 'description': 'Description', 'tags': ['topic']} for k in ('long', 'short')}
            (project/'metadata.json').write_text(json.dumps(meta))
            events = []
            yt = Mock()
            def upload(path, snippet, upload_state, save, **kwargs):
                events.append('upload-'+snippet['title'])
                upload_state['id'] = snippet['title']+'-id'
                return upload_state['id']
            yt.upload.side_effect = upload
            yt.schedule.side_effect = lambda video_id, stamp, **kw: events.append('schedule-'+video_id) or stamp
            states = {'imax-001': {'status': 'ready'}}
            with patch('automation.queue_runner.YouTube', return_value=yt), \
                 patch('automation.queue_runner.email_summary', side_effect=lambda *args: events.append('email')):
                process(item(), {'email_mode': 'confirmation'}, root, states, Mock())
            self.assertEqual(events, ['upload-long', 'upload-short', 'schedule-long-id', 'schedule-short-id', 'email'])
            self.assertEqual(states['imax-001']['status'], 'done')

    def test_wrong_approval_cannot_publish(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.review_digest', return_value='current'), \
             patch('automation.queue_runner.email_summary'), patch('automation.queue_runner.YouTube') as yt, \
             patch.dict('os.environ', {'APPROVE_ITEM_ID': 'imax-001', 'APPROVAL_CODE': 'stale'}):
            states = {'imax-001': {'status': 'ready', 'review_digest': 'current', 'email_review': 'sent'}}
            with self.assertRaisesRegex(ValueError, 'Approval does not match'):
                process(item(), {'email_mode': 'review'}, Path(tmp), states, Mock())
            yt.assert_not_called()

    def test_changed_package_revokes_approval(self):
        with tempfile.TemporaryDirectory() as tmp, patch('automation.queue_runner.review_digest', return_value='new'), \
             patch('automation.queue_runner.email_summary'), patch('automation.queue_runner.YouTube') as yt, \
             patch.dict('os.environ', {'APPROVE_ITEM_ID': '', 'APPROVAL_CODE': ''}):
            states = {'imax-001': {'status': 'ready', 'review_digest': 'old', 'approved_digest': 'old', 'email_review': 'sent'}}
            process(item(), {'email_mode': 'review'}, Path(tmp), states, Mock())
            self.assertNotIn('approved_digest', states['imax-001'])
            self.assertEqual(states['imax-001']['status'], 'awaiting_approval')
            yt.assert_not_called()

    def test_regeneration_archives_and_resets_approval_only_once(self):
        from automation.queue_runner import apply_regeneration
        state = {'status': 'awaiting_approval', 'review_digest': 'v1', 'email_review': 'sent',
                 'approved_digest': 'v1', 'signature': 'idea'}
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate', 'APPROVE_ITEM_ID': 'imax-001', 'APPROVAL_CODE': 'v1'}):
            save = Mock()
            apply_regeneration(item(), state, save)
            self.assertEqual(state['revision'], 2)
            self.assertNotIn('approved_digest', state)
            self.assertNotIn('email_review', state)
            self.assertEqual(state['history'][0]['review_digest'], 'v1')
            apply_regeneration(item(), state, save)
            self.assertEqual(state['revision'], 2)
            self.assertEqual(save.call_count, 1)

    def test_regeneration_rejects_stale_code_and_uploaded_video(self):
        from automation.queue_runner import apply_regeneration
        with patch.dict('os.environ', {'REVIEW_ACTION': 'regenerate', 'APPROVE_ITEM_ID': 'imax-001', 'APPROVAL_CODE': 'old'}):
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
            with patch.dict('os.environ', {'REVIEW_ONLY':'1', 'APPROVE_ITEM_ID':item()['id'], 'APPROVAL_CODE':'digest'}), \
                 patch.object(q, 'review_digest', return_value='digest'), \
                 patch.object(q, 'email_summary'), patch.object(q, 'YouTube') as youtube:
                process(item(), {'email_mode':'confirmation'}, root, states, Mock())
            youtube.assert_not_called()
            self.assertEqual(states[item()['id']]['status'], 'awaiting_approval')
