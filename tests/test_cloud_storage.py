from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from automation.cloud_storage import CloudStore, checkpoint


class CloudStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bucket = Mock()
        self.store = CloudStore(self.bucket, self.root)

    def test_checkpoint_failure_stops_caller(self):
        path = self.root / 'job.json'
        path.write_text('{"status":"SUBMITTING"}')
        self.bucket.blob.return_value.upload_from_filename.side_effect = RuntimeError('offline')
        with patch('automation.cloud_storage.get_store', return_value=self.store):
            with self.assertRaises(RuntimeError):
                checkpoint(path)
        self.assertEqual(self.store.known, {})

    def test_state_is_written_after_assets_and_temporary_files_skipped(self):
        for name in ('state.json', 'video.mp4', 'render.partial.mp4', 'x.tmp', 'queue.lock'):
            (self.root/name).write_text('data')
        with patch('automation.cloud_storage.get_store', return_value=self.store):
            checkpoint(self.root/'state.json')
        names = [c.args[0] for c in self.bucket.blob.call_args_list]
        self.assertEqual(names, ['pipeline/video.mp4', 'pipeline/state.json'])
        self.store.sync()
        self.assertEqual(self.bucket.blob.call_count, 2)

    def test_restore_rejects_path_traversal(self):
        self.bucket.list_blobs.return_value = [Mock(name='blob')]
        self.bucket.list_blobs.return_value[0].name = 'pipeline/../outside'
        with self.assertRaises(RuntimeError):
            self.store.restore()

    def test_discard_project_removes_all_revisions_and_cloud_objects(self):
        directory = self.root/'projects'/'idea-1'/'revision-002'
        directory.mkdir(parents=True); (directory/'video.mp4').write_bytes(b'video')
        self.store.known['projects/idea-1/revision-002/video.mp4'] = (1, 5)
        blobs = [Mock(generation=10), Mock(generation=11)]
        self.bucket.list_blobs.return_value = blobs
        self.store.discard_project('idea-1')
        self.bucket.list_blobs.assert_called_once_with(prefix='pipeline/projects/idea-1/')
        for blob in blobs:
            blob.delete.assert_called_once_with(if_generation_match=blob.generation)
        self.assertFalse((self.root/'projects'/'idea-1').exists())
        self.assertEqual(self.store.known, {})

    def test_discard_output_deletes_only_selected_cloud_objects(self):
        project = self.root/'projects'/'idea-1'/'revision-002'
        project.mkdir(parents=True)
        names = ['video.mp4', 'assets/scene.png', 'thumbnail-history/old.jpg',
                 'short/video.mp4', 'short/assets/scene.png']
        blobs = []
        for index, name in enumerate(names, 1):
            blob = Mock(generation=index)
            blob.name = 'pipeline/projects/idea-1/revision-002/' + name
            blobs.append(blob)
            self.store.known['projects/idea-1/revision-002/' + name] = (1, index)
        self.bucket.list_blobs.return_value = blobs
        self.store.discard_output(project, 'long')
        self.assertTrue(all(blobs[index].delete.called for index in (0, 1)))
        self.assertTrue(all(not blobs[index].delete.called for index in (2, 3, 4)))
        self.assertNotIn('projects/idea-1/revision-002/video.mp4', self.store.known)
        self.assertIn('projects/idea-1/revision-002/short/video.mp4', self.store.known)

    def test_cleanup_only_old_completed_uploaded_items(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=8)).isoformat()
        ready = {'status':'done', 'completed_at':old,
                 'uploads':{k:{'id':k} for k in ('long','short')}}
        states = {'old':ready, 'pending':{'status':'awaiting_approval'},
                  'recent':{**ready, 'completed_at':now.isoformat()},
                  'partial':{**ready, 'uploads':{'long':{'id':'long'}}}}
        directory = self.root/'projects'/'old'; directory.mkdir(parents=True)
        (directory/'video.mp4').write_bytes(b'video')
        blob = Mock(); blob.generation = 42
        self.bucket.list_blobs.return_value = [blob]
        save = Mock()
        self.store.cleanup(states, save, now)
        self.bucket.list_blobs.assert_called_once_with(prefix='pipeline/projects/old/')
        blob.delete.assert_called_once_with(if_generation_match=42)
        self.assertFalse(directory.exists())
        self.assertEqual(states['old']['uploads']['long']['id'], 'long')
        self.assertNotIn('media_deleted_at',states['pending'])
        save.assert_called_once()

    def test_restore_does_not_merge_unknown_local_state(self):
        (self.root/'state.json').write_text('{}')
        with self.assertRaises(RuntimeError):
            self.store.restore()
        self.bucket.list_blobs.assert_not_called()

    def test_lock_rejects_second_owner_before_restore(self):
        from automation.cloud_storage import CloudSession
        storage = Mock()
        storage.Client.return_value.bucket.return_value = self.bucket
        lock = self.bucket.blob.return_value
        lock.upload_from_string.side_effect = RuntimeError('lock exists')
        with patch.dict('sys.modules', {'google.cloud': SimpleNamespace(storage=storage)}), \
             patch.dict('os.environ', {'GCS_BUCKET':'test', 'VIDEO_WORK_ROOT':str(self.root)}):
            with self.assertRaises(RuntimeError):
                with CloudSession():
                    self.fail('A competing session entered')
        self.assertEqual(lock.upload_from_string.call_args.kwargs['if_generation_match'], 0)
        lock.delete.assert_not_called()
        self.bucket.list_blobs.assert_not_called()

    def test_lock_released_on_processing_error(self):
        from automation.cloud_storage import CloudSession
        storage = Mock()
        storage.Client.return_value.bucket.return_value = self.bucket
        self.bucket.list_blobs.return_value = []
        lock = self.bucket.blob.return_value
        lock.generation = 123
        with patch.dict('sys.modules', {'google.cloud': SimpleNamespace(storage=storage)}), \
             patch.dict('os.environ', {'GCS_BUCKET':'test', 'VIDEO_WORK_ROOT':str(self.root)}):
            with self.assertRaises(ValueError):
                with CloudSession():
                    raise ValueError('processing failed')
        lock.delete.assert_called_once_with(if_generation_match=123)


if __name__ == '__main__':
    unittest.main()
