"""Delete recorded test uploads and generated cloud state after explicit confirmation."""
import argparse
import json
import os

import requests


def video_ids(state):
    return sorted({upload['id'] for item in state.values()
                   for upload in item.get('uploads', {}).values()
                   if isinstance(upload, dict) and upload.get('id')})


def safe_reason(response):
    try:
        error = response.json().get('error', {})
        return ', '.join(entry.get('reason', '') for entry in error.get('errors', []) if entry.get('reason'))
    except (TypeError, ValueError):
        return ''


def main():
    from google.cloud import storage
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm', required=True)
    args = parser.parse_args()
    if args.confirm != 'DELETE TEST DATA':
        raise ValueError('Confirmation must exactly match DELETE TEST DATA.')
    bucket = storage.Client().bucket(os.environ['GCS_BUCKET'])
    state_blob = bucket.blob('pipeline/state.json')
    state = json.loads(state_blob.download_as_text()) if state_blob.exists() else {}
    ids = video_ids(state)
    if ids:
        token = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': os.environ['YOUTUBE_CLIENT_ID'],
            'client_secret': os.environ['YOUTUBE_CLIENT_SECRET'],
            'refresh_token': os.environ['YOUTUBE_REFRESH_TOKEN'],
            'grant_type': 'refresh_token'}, timeout=60)
        token.raise_for_status()
        headers = {'Authorization': 'Bearer ' + token.json()['access_token']}
        for video_id in ids:
            response = requests.delete('https://www.googleapis.com/youtube/v3/videos',
                                       params={'id': video_id}, headers=headers, timeout=60)
            if response.status_code not in (204, 404):
                reason = safe_reason(response)
                raise RuntimeError(f'YouTube deletion failed: HTTP {response.status_code}' +
                                   (f' ({reason})' if reason else ''))
    blobs = list(bucket.list_blobs(prefix='pipeline/'))
    lock = bucket.blob('locks/queue.json')
    if lock.exists():
        blobs.append(lock)
    for blob in blobs:
        blob.delete(if_generation_match=blob.generation)
    print(f'Deleted {len(ids)} recorded YouTube test video(s) and {len(blobs)} cloud test object(s).')


if __name__ == '__main__':
    main()
