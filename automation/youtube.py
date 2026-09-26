"""Persisted YouTube resumable uploads. Never silently restart an expired upload."""
import hashlib
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import requests


def future_time(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt < datetime.now(timezone.utc) + timedelta(minutes=15):
        raise ValueError('publish_at must include a timezone and be at least 15 minutes in the future.')
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


class YouTube:
    def __init__(self):
        self.session = requests.Session()
        response = self.session.post('https://oauth2.googleapis.com/token', data={
            'client_id': os.environ['YOUTUBE_CLIENT_ID'], 'client_secret': os.environ['YOUTUBE_CLIENT_SECRET'],
            'refresh_token': os.environ['YOUTUBE_REFRESH_TOKEN'], 'grant_type': 'refresh_token'}, timeout=60)
        self.check(response)
        self.session.headers['Authorization'] = 'Bearer ' + response.json()['access_token']

    @staticmethod
    def check(response):
        if not response.ok:
            # Only expose Google's machine-readable reason/status allowlist. Never echo
            # provider messages, request URLs, access tokens or upload-session URLs.
            details = []
            try:
                error = response.json().get('error', {})
                if isinstance(error, dict):
                    details.extend(entry.get('reason') for entry in error.get('errors', [])
                                   if isinstance(entry, dict) and entry.get('reason'))
                    if error.get('status'):
                        details.append(error['status'])
                elif isinstance(error, str):
                    details.append(error)
            except (TypeError, ValueError):
                pass
            safe = ', '.join(dict.fromkeys(str(value)[:80] for value in details))
            suffix = f' ({safe})' if safe else ''
            raise RuntimeError(f'YouTube/OAuth request failed: HTTP {response.status_code}{suffix}. '
                               'Check credentials, quota and channel permissions.')

    def upload(self, path, metadata, state, save, *, made_for_kids, synthetic):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        signature = hashlib.sha256((digest + str(metadata) + str(made_for_kids) + str(synthetic)).encode()).hexdigest()
        if state.get('signature') and state['signature'] != signature:
            raise ValueError('Video or metadata changed after upload started. Reconcile the saved YouTube upload before continuing.')
        if state.get('id'):
            return state['id']
        size = path.stat().st_size
        if not state.get('session'):
            if state.get('started'):
                raise RuntimeError('Upload initiation outcome is unknown. Inspect YouTube Studio before clearing this upload state.')
            state.update(started=True, signature=signature); save()
            r = self.session.post('https://www.googleapis.com/upload/youtube/v3/videos',
                params={'uploadType': 'resumable', 'part': 'snippet,status'},
                headers={'X-Upload-Content-Length': str(size), 'X-Upload-Content-Type': 'video/mp4'},
                json={'snippet': metadata, 'status': {'privacyStatus': 'private',
                      'selfDeclaredMadeForKids': made_for_kids, 'containsSyntheticMedia': synthetic}}, timeout=60)
            try:
                self.check(r)
            except RuntimeError:
                # A received HTTP rejection proves no resumable session was created.
                # Clear only this initiation marker so a corrected authorization can retry.
                state.pop('started', None); state.pop('signature', None); save()
                raise
            url = r.headers['Location']
            if urlparse(url).scheme != 'https' or urlparse(url).hostname != 'www.googleapis.com':
                raise RuntimeError('Unexpected upload session host.')
            state['session'] = url; save()
        url = state['session']
        r = self.session.put(url, headers={'Content-Range': f'bytes */{size}', 'Content-Length': '0'}, data=b'', timeout=60)
        with path.open('rb') as video:
            while True:
                if r.status_code in (200, 201):
                    state['id'] = r.json()['id']; state.pop('session', None); save()
                    return state['id']
                if r.status_code != 308:
                    if r.status_code in (404, 410):
                        raise RuntimeError('Upload session expired. Inspect YouTube Studio; do not automatically submit a duplicate.')
                    self.check(r)
                    raise RuntimeError('Unexpected resumable-upload response.')
                offset = int(r.headers['Range'].split('-')[-1]) + 1 if r.headers.get('Range') else 0
                if offset >= size:
                    raise RuntimeError('Upload has all bytes but no video ID; retry status later.')
                video.seek(offset); chunk = video.read(8 * 1024 * 1024)
                r = self.session.put(url, data=chunk, headers={'Content-Type': 'video/mp4',
                    'Content-Range': f'bytes {offset}-{offset + len(chunk)-1}/{size}'}, timeout=(15, 300))

    def thumbnail(self, video_id, path):
        with path.open('rb') as image:
            r = self.session.post('https://www.googleapis.com/upload/youtube/v3/thumbnails/set',
                params={'videoId': video_id, 'uploadType': 'media'},
                headers={'Content-Type': 'image/jpeg'}, data=image, timeout=120)
        self.check(r)

    def schedule(self, video_id, publish_at, *, made_for_kids, synthetic):
        stamp = future_time(publish_at)
        r = self.session.put('https://www.googleapis.com/youtube/v3/videos', params={'part': 'status'},
            json={'id': video_id, 'status': {'privacyStatus': 'private', 'publishAt': stamp,
                 'selfDeclaredMadeForKids': made_for_kids, 'containsSyntheticMedia': synthetic}}, timeout=60)
        self.check(r)
        status = r.json().get('status', {})
        if not status.get('publishAt'):
            raise RuntimeError('YouTube did not confirm a publishing time; inspect Studio.')
        return status['publishAt']
