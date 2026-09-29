"""Private GCS checkpoints for disposable runners. Disabled for ordinary local runs."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

_store = None


def is_precondition_failed(exc):
    """Recognize GCS generation conflicts without importing its optional SDK."""
    code = getattr(exc, 'code', None)
    return type(exc).__name__ == 'PreconditionFailed' or code == 412 or getattr(code, 'value', None) == 412


class CloudStore:
    def __init__(self, bucket, root):
        self.bucket = bucket
        self.root = Path(root).resolve()
        self.known = {}

    def relative(self, path):
        path = Path(path).resolve()
        return path.relative_to(self.root).as_posix()

    def checkpoint(self, path):
        path = Path(path)
        name = self.relative(path)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if self.known.get(name) == signature:
            return
        self.bucket.blob('pipeline/' + name).upload_from_filename(str(path), timeout=120)
        self.known[name] = signature

    def sync(self):
        # Assets must reach durable storage before queue state declares them ready.
        for path in sorted(self.root.rglob('*')):
            if (not path.is_file() or path.is_symlink() or path.name in ('state.json', 'queue.lock')
                    or path.suffix in ('.tmp', '.mkv') or '.partial' in path.name):
                continue
            self.checkpoint(path)

    def restore(self):
        if any(self.root.iterdir()):
            raise RuntimeError('Cloud restore requires an empty work directory.')
        for blob in self.bucket.list_blobs(prefix='pipeline/'):
            name = blob.name[len('pipeline/'):]
            target = (self.root / name).resolve()
            if not name or not target.is_relative_to(self.root) or target == self.root:
                raise RuntimeError('Invalid cloud checkpoint path.')
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target), timeout=120)
            stat = target.stat()
            self.known[name] = (stat.st_mtime_ns, stat.st_size)

    def discard_output(self, project, kind):
        """Delete one generated output while preserving the other output in the same package."""
        if kind not in ('long', 'short'):
            raise ValueError('Output kind must be long or short.')
        project = Path(project).resolve()
        relative = self.relative(project).rstrip('/')
        prefix = 'pipeline/' + relative + '/'
        for blob in self.bucket.list_blobs(prefix=prefix):
            remainder = blob.name[len(prefix):]
            selected = remainder.startswith('short/') if kind == 'short' else not (
                remainder.startswith('short/') or remainder.startswith('thumbnail-history/')
                or remainder in ('metadata.json', 'metadata-source.json'))
            if selected:
                blob.delete(if_generation_match=blob.generation)
                self.known.pop(relative + '/' + remainder, None)

    def discard_audio_output(self, project, kind):
        """Delete selected audio/render checkpoints while retaining scripts and imagery."""
        if kind not in ('long', 'short'):
            raise ValueError('Audio output kind must be long or short.')
        relative = self.relative(Path(project).resolve()).rstrip('/')
        prefix = 'pipeline/' + relative + '/'
        exact_names = {
            'video.mp4', 'video.partial.mp4', 'video.narration.mp4', 'soundtrack.partial.mp4',
            'soundtrack-mix.json', 'soundtrack.json', 'captions.srt', 'captions.ass',
            'narration-quality.json', 'transcript.json', 'short-timing.json',
        }
        for blob in self.bucket.list_blobs(prefix=prefix):
            remainder = blob.name[len(prefix):]
            path = Path(remainder)
            in_short = path.parts[0] == 'short'
            if (kind == 'long' and in_short) or (kind == 'short' and not in_short):
                continue
            audio_asset = (path.parent.name == 'assets' and
                           (path.suffix.lower() in ('.wav', '.mp3') or
                            path.name.endswith(('.audio.json', '.runpod-job.json')) or
                            path.name.startswith('narration-quality')))
            if path.name in exact_names or audio_asset:
                blob.delete(if_generation_match=blob.generation)
                self.known.pop(relative + '/' + remainder, None)

    def discard_project(self, item_id):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', item_id):
            raise ValueError('Invalid stored queue ID.')
        prefix = 'pipeline/projects/' + item_id + '/'
        for blob in self.bucket.list_blobs(prefix=prefix):
            blob.delete(if_generation_match=blob.generation)
        shutil.rmtree(self.root / 'projects' / item_id, ignore_errors=True)
        for name in [name for name in self.known if name.startswith('projects/' + item_id + '/')]:
            self.known.pop(name, None)

    def cleanup(self, states, save, now=None):
        now = now or datetime.now(timezone.utc)
        for item_id, state in states.items():
            if state.get('status') != 'done' or state.get('media_deleted_at'):
                continue
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', item_id):
                raise ValueError('Invalid stored queue ID.')
            completed = datetime.fromisoformat(state['completed_at'])
            if now - completed < timedelta(days=7):
                continue
            if not all(state.get('uploads', {}).get(k, {}).get('id') for k in ('long', 'short')):
                continue
            prefix = 'pipeline/projects/' + item_id + '/'
            for blob in self.bucket.list_blobs(prefix=prefix):
                blob.delete(if_generation_match=blob.generation)
            shutil.rmtree(self.root / 'projects' / item_id, ignore_errors=True)
            state['media_deleted_at'] = now.isoformat()
            save()  # Keep job history and YouTube IDs permanently in state.json.


class CloudSession:
    def __enter__(self):
        global _store
        from google.cloud import storage
        bucket = storage.Client().bucket(os.environ['GCS_BUCKET'])
        self.lock = bucket.blob('locks/queue.json')
        self.token = uuid.uuid4().hex
        record = {'token': self.token, 'run_id': os.getenv('GITHUB_RUN_ID'),
                  'created_at': datetime.now(timezone.utc).isoformat()}
        try:
            self.lock.upload_from_string(json.dumps(record), content_type='application/json',
                                         if_generation_match=0)
        except Exception as exc:
            current_run = os.getenv('GITHUB_RUN_ID')
            if not is_precondition_failed(exc) or os.getenv('GITHUB_ACTIONS') != 'true' or not current_run:
                raise RuntimeError('Cloud queue lock is already held; confirm its recorded run is finished before retrying.') from exc
            self.lock.reload()
            generation = self.lock.generation
            try:
                owner = json.loads(self.lock.download_as_text())
            except (TypeError, ValueError):
                owner = {}
            owner_run = str(owner.get('run_id') or '')
            if not owner_run or owner_run == current_run:
                raise RuntimeError('Cloud queue lock ownership is ambiguous; inspect locks/queue.json before retrying.') from exc
            # GitHub serializes the video-production-queue concurrency group. If
            # this job is running, the different recorded workflow run cannot
            # still be active, so its create-only lock is an interrupted-run remnant.
            self.lock.delete(if_generation_match=generation)
            print(f'Removed stale cloud queue lock from completed workflow run {owner_run}.', flush=True)
            try:
                self.lock.upload_from_string(json.dumps(record), content_type='application/json',
                                             if_generation_match=0)
            except Exception as retry_exc:
                if is_precondition_failed(retry_exc):
                    raise RuntimeError('Another worker acquired the cloud queue lock during recovery; retry later.') from retry_exc
                raise
        self.generation = self.lock.generation
        try:
            root = Path(os.environ['VIDEO_WORK_ROOT']).resolve()
            root.mkdir(parents=True, exist_ok=True)
            _store = CloudStore(bucket, root)
            _store.restore()
            return _store
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        global _store
        _store = None
        self.lock.delete(if_generation_match=self.generation)


def get_store():
    global _store
    if not os.getenv('VIDEO_CLOUD_CHECKPOINTS'):
        return None
    if _store is None:  # Pipeline subprocess inherits the parent's locked session.
        from google.cloud import storage
        _store = CloudStore(storage.Client().bucket(os.environ['GCS_BUCKET']), os.environ['VIDEO_WORK_ROOT'])
    return _store


def checkpoint(path):
    store = get_store()
    if store:
        if store.relative(path) == 'state.json':
            store.sync()
        store.checkpoint(path)
