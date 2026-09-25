"""One-time local OAuth consent; never run in Actions. Requires google-auth-oauthlib."""
import argparse
import json
import os
from pathlib import Path


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-secrets', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('Output already exists; choose a new private path.')
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets),
        scopes=['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.force-ssl'])
    credentials = flow.run_local_server(port=0, access_type='offline', prompt='consent',
        success_message='Google authorization received. Return to the assistant to confirm credentials were saved.')
    if not credentials.refresh_token:
        raise RuntimeError('Google did not return a refresh token.')
    with open(args.out, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump({'YOUTUBE_CLIENT_ID': credentials.client_id, 'YOUTUBE_CLIENT_SECRET': credentials.client_secret,
                   'YOUTUBE_REFRESH_TOKEN': credentials.refresh_token}, output, indent=2)
    print('Credentials saved locally. Add the three values to GitHub Secrets; never commit this file.')


if __name__ == '__main__':
    main()
