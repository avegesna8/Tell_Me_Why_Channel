"""Atomically remove a completed queue item from the latest GitHub queue."""
import argparse
import base64
import json
import os
import re
from pathlib import PurePosixPath

import requests

from automation.queue_runner import validate_queue


def remove_item(raw, item_id):
    queue = validate_queue(json.loads(raw))
    before = len(queue['items'])
    queue['items'] = [item for item in queue['items'] if item['id'] != item_id]
    if len(queue['items']) == before:
        return None
    return json.dumps(queue, indent=2, ensure_ascii=False) + '\n'


def archive_completed(repo, path, item_id, token, branch='main', attempts=3):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid GitHub repository name.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', item_id):
        raise ValueError('Invalid completed item ID.')
    clean_path = str(PurePosixPath(path))
    if clean_path.startswith('../') or clean_path.startswith('/'):
        raise ValueError('Invalid queue path.')
    url = f'https://api.github.com/repos/{repo}/contents/{clean_path}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    for attempt in range(attempts):
        current = requests.get(url, headers=headers, params={'ref': branch}, timeout=(15, 45))
        if not current.ok:
            raise RuntimeError(f'GitHub queue read failed with HTTP {current.status_code}.')
        record = current.json()
        try:
            encoded = record['content']
            if not isinstance(encoded, str):
                raise TypeError('content is not text')
            # GitHub's Contents API wraps Base64 at fixed-width line boundaries.
            raw = base64.b64decode(''.join(encoded.split()), validate=True).decode()
            updated = remove_item(raw, item_id)
            sha = record['sha']
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError('GitHub returned invalid queue content.') from exc
        if updated is None:
            print(f'Completed queue item {item_id} was already archived.')
            return False
        payload = {'message': f'Archive completed video idea {item_id}',
                   'content': base64.b64encode(updated.encode()).decode(),
                   'sha': sha, 'branch': branch}
        saved = requests.put(url, headers=headers, json=payload, timeout=(15, 45))
        if saved.ok:
            print(f'Archived completed queue item {item_id}.')
            return True
        if saved.status_code not in (409, 422) or attempt + 1 == attempts:
            raise RuntimeError(f'GitHub queue update failed with HTTP {saved.status_code}.')
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=os.getenv('GITHUB_REPOSITORY', ''))
    parser.add_argument('--path', default='video_queue.json')
    parser.add_argument('--item-id', required=True)
    parser.add_argument('--branch', default='main')
    args = parser.parse_args()
    token = os.getenv('GITHUB_TOKEN', '')
    if not token:
        raise ValueError('GITHUB_TOKEN is required to archive a completed item.')
    archive_completed(args.repository, args.path, args.item_id, token, args.branch)


if __name__ == '__main__':
    main()
