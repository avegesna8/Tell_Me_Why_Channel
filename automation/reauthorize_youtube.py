"""Authorize the intended YouTube channel and replace its GitHub Actions secrets."""
import argparse
import json
from pathlib import Path
import subprocess

import requests


SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.force-ssl',
]


def discover_client_secrets(downloads=None):
    root = Path(downloads or Path.home() / 'Downloads').expanduser()
    candidates = sorted(root.glob('client_secret*.json'), key=lambda path: path.stat().st_mtime,
                        reverse=True)
    for path in candidates:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get('installed'), dict):
            return path
    raise FileNotFoundError('No Desktop OAuth client_secret*.json was found in Downloads.')


def authorized_channel(access_token, *, session=requests):
    response = session.get('https://www.googleapis.com/youtube/v3/channels',
                           params={'part': 'snippet', 'mine': 'true'},
                           headers={'Authorization': 'Bearer ' + access_token}, timeout=30)
    response.raise_for_status()
    items = response.json().get('items', [])
    if len(items) != 1:
        raise RuntimeError(f'Expected one authorized YouTube channel; Google returned {len(items)}.')
    return {'id': items[0].get('id', ''),
            'title': items[0].get('snippet', {}).get('title', '')}


def update_github_secrets(repository, credentials, *, run=subprocess.run):
    values = {
        'YOUTUBE_CLIENT_ID': credentials.client_id,
        'YOUTUBE_CLIENT_SECRET': credentials.client_secret,
        'YOUTUBE_REFRESH_TOKEN': credentials.refresh_token,
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise RuntimeError('Google did not return a complete reusable credential set.')
    run(['gh', 'auth', 'status', '--hostname', 'github.com'], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for name, value in values.items():
        # The secret travels on stdin so it never appears in the command line or logs.
        run(['gh', 'secret', 'set', name, '--repo', repository], input=value, text=True, check=True)


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-secrets', type=Path)
    parser.add_argument('--repository', default='avegesna8/Tell_Me_Why_Channel')
    parser.add_argument('--expected-channel-id', default='UCdnHeM7WcBmg2YvbobsvCcg')
    parser.add_argument('--expected-channel-name', default='Tell Me Why')
    args = parser.parse_args()

    try:
        client_file = args.client_secrets.expanduser() if args.client_secrets else discover_client_secrets()
    except FileNotFoundError:
        print('Missing Google Desktop OAuth client JSON.', flush=True)
        print('1. Open Google Cloud Console → APIs & Services → Credentials.', flush=True)
        print('2. Download the JSON for the Desktop app OAuth 2.0 Client ID.', flush=True)
        print('3. Leave client_secret*.json in Downloads and run this launcher again.', flush=True)
        raise SystemExit(2)
    if not client_file.is_file():
        raise FileNotFoundError(f'OAuth client file not found: {client_file}')
    print(f'Using Desktop OAuth client: {client_file.name}', flush=True)
    print(f'In the browser, choose the {args.expected_channel_name} Brand Channel.', flush=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes=SCOPES)
    credentials = flow.run_local_server(
        port=0, access_type='offline', prompt='consent',
        success_message='Google authorization received. You may return to the Terminal window.')
    if not credentials.refresh_token:
        raise RuntimeError('Google did not return a refresh token. Revoke the old app grant and try again.')

    channel = authorized_channel(credentials.token)
    if channel['id'] != args.expected_channel_id:
        shown = channel['title'] or '(untitled channel)'
        raise RuntimeError(f'Wrong YouTube channel selected: {shown}. No GitHub secrets were changed. '
                           f'Run this command again and choose {args.expected_channel_name}.')
    update_github_secrets(args.repository, credentials)
    print(f'Connected GitHub Actions to YouTube channel: {channel["title"]} ({channel["id"]})', flush=True)
    print('YouTube reauthorization is complete. You can rerun the failed approval workflow.', flush=True)


if __name__ == '__main__':
    main()
