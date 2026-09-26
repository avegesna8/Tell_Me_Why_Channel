"""Process one FIFO item with local or cloud-backed progress. Run as a module."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from email.message import EmailMessage
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
import sys
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import pipeline as p
from automation.production import metadata, thumbnail
from automation.youtube import YouTube


def validate_queue(value):
    ZoneInfo(value.get('timezone', 'America/New_York'))
    if value.get('email_mode') not in ('review', 'confirmation'):
        raise ValueError('email_mode must be review or confirmation.')
    ids = set()
    for item in value['items']:
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', item['id']) or item['id'] in ids:
            raise ValueError('Queue IDs must be unique stable lowercase slugs.')
        ids.add(item['id'])
        if not isinstance(item.get('idea'), str) or not item['idea'].strip():
            raise ValueError('Each queued item needs an idea.')
        if 'notes' in item and not isinstance(item['notes'], str):
            raise ValueError('Optional notes must be text.')
        if not .25 <= item.get('minutes', 3) <= 30:
            raise ValueError('minutes must be 0.25–30.')
        if type(item.get('short_seconds', 55)) is not int or not 30 <= item.get('short_seconds', 55) <= 60:
            raise ValueError('short_seconds must be an integer between 30 and 60.')
        for key in ('made_for_kids', 'contains_synthetic_media'):
            if not isinstance(item.get(key), bool):
                raise ValueError(f'Explicit {key} true/false is required.')
        if not isinstance(item.get('approved', False), bool) or not isinstance(item.get('enabled', True), bool):
            raise ValueError('approved/enabled must be booleans.')
    return value



def choose_item(queue, state):
    return next((i for i in queue['items'] if i.get('enabled', True)
                 and state.get(i['id'], {}).get('status') != 'done'), None)


def review_digest(item, project):
    files = ['video.mp4', 'short/video.mp4', 'metadata.json', 'thumbnail.jpg', 'script.txt', 'short/script.txt', 'script-quality.json']
    files += [name for name in ('speech-text.txt', 'speech-preparation.json', 'production-review.json',
                                     'short/production-review.json') if (project / name).exists()]
    hashes = {}
    for name in files:
        with (project / name).open('rb') as stream:
            hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return p.fingerprint({'version_folder': project.name, 'files': hashes, 'item': {k: item.get(k) for k in
        # Retain legacy empty schedule keys so already-approved packages keep their digest.
        ('id', 'publish_at', 'short_publish_at', 'made_for_kids', 'contains_synthetic_media')}})


def email_summary(item, project, state, save, kind):
    key = 'email_' + kind
    if state.get(key) == 'sent':
        return
    if state.get(key) == 'sending':
        raise RuntimeError('Previous email delivery outcome is unknown. Check inbox before changing its state; no duplicate email sent.')
    message = EmailMessage()
    message['From'] = os.environ['SMTP_FROM']; message['To'] = os.environ['SUMMARY_EMAIL_TO']
    message['Subject'] = f"Video {kind}: {item['id']}"
    message['Message-ID'] = f"<{item['id']}.v{state.get('revision', 1)}.{kind}@video-automation.local>"
    report = {'idea': item['idea'], 'metadata': json.loads((project/'metadata.json').read_text()),
              'youtube': {k: {'url': 'https://www.youtube.com/watch?v='+v['id'], 'privacy_status': 'private'}
                          for k, v in state.get('uploads', {}).items() if v.get('id')},
              'workflow_run': ('https://github.com/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID'])
                              if os.getenv('GITHUB_RUN_ID') else None}
    report['soundtracks'] = {kind: json.loads((location/'soundtrack.json').read_text())
                            for kind, location in [('long',project),('short',project/'short')]
                            if (location/'soundtrack.json').exists()}
    if (project/'script-quality.json').exists():
        report['script_quality'] = json.loads((project/'script-quality.json').read_text())
    if state.get('thumbnail_error'):
        report['thumbnail_warning'] = state['thumbnail_error']
    approval_url = ('https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/workflows/video-queue.yml') if os.getenv('GITHUB_REPOSITORY') else None
    if kind == 'review':
        report['approval'] = {'workflow': approval_url, 'approval_code': state['review_digest'],
                              'revision': state.get('revision', 1), 'actions': ['approve', 'regenerate']}
    p.save_json(project / 'upload-details.json', report)
    videos = [project/'video.mp4', project/'short/video.mp4']
    attach_videos = sum(file.stat().st_size for file in videos) <= 14 * 1024 * 1024
    video_note = ('Both videos are attached.' if attach_videos else
                  'Download both videos from the video-review artifact on the workflow-run link below. '
                  'The artifact appears after this workflow finishes; sign in to GitHub to access it.')
    message.set_content(video_note + '\n\n' + json.dumps(report, indent=2) +
        ('\n\nOpen the workflow link, click Run workflow, paste the approval code above, then choose review_action: '
         'approve to upload both videos privately, or regenerate to create a completely new version of this idea. '
         'Regenerate recreates the script, both videos, images, thumbnail and metadata, and sends a new review email. '
         'Previous versions are preserved. Replies to this email are not processed.' if kind == 'review' else ''))
    for file in [project/'upload-details.json', project/'thumbnail.jpg', project/'script.txt', project/'short/script.txt',
                 project/'script-quality.json', project/'speech-text.txt', project/'speech-preparation.json',
                 project/'production-review.json', project/'short/production-review.json']:
        if not file.exists():
            continue
        mime = ('image', 'jpeg') if file.suffix == '.jpg' else ('application', 'json') if file.suffix == '.json' else ('text', 'plain')
        message.add_attachment(file.read_bytes(), maintype=mime[0], subtype=mime[1],
                               filename=('short-' if file.parent.name == 'short' else '') + file.name)
    if attach_videos:
        for file in videos:
            message.add_attachment(file.read_bytes(), maintype='video', subtype='mp4',
                                   filename='short-video.mp4' if file.parent.name == 'short' else 'long-video.mp4')
    # Establish/authenticate before recording an ambiguous send attempt.
    with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT', '587')), timeout=60) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(os.environ['SMTP_USERNAME'], os.environ['SMTP_PASSWORD'])
        state[key] = 'sending'; save()
        client.send_message(message)
        state[key] = 'sent'; save()


def apply_regeneration(item, state, save):
    if os.getenv('REVIEW_ACTION', 'approve') != 'regenerate':
        return
    code = os.getenv('APPROVAL_CODE', '')
    if not code:
        raise ValueError('Regeneration requires the code from its review email.')
    if code in state.get('regeneration_requests', []):
        return  # Retried dispatch resumes that version, not another paid generation.
    if state.get('uploads') or state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError('Regenerate is available before uploading. Reconcile existing YouTube uploads first.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError('Regeneration code is stale or has not been emailed.')
    history = state.get('history', []) + [{k: v for k, v in state.items() if k != 'history'}]
    revision = state.get('revision', 1) + 1
    requests = state.get('regeneration_requests', []) + [code]
    signature = state.get('signature')
    state.clear()
    state.update(revision=revision, status='generating', history=history,
                 regeneration_requests=requests, signature=signature)
    save()


def process(item, queue, root, states, save):
    state = states.setdefault(item['id'], {})
    signature = p.fingerprint({k: item.get(k) for k in ('idea', 'notes', 'minutes', 'made_for_kids', 'contains_synthetic_media')})
    legacy_signature = p.fingerprint({k: item.get(k) for k in ('idea', 'minutes', 'made_for_kids', 'contains_synthetic_media')})
    if 'short_seconds' in item:
        signature = p.fingerprint({'base': signature, 'short_seconds': item['short_seconds']})
        legacy_signature = p.fingerprint({'base': legacy_signature, 'short_seconds': item['short_seconds']})
    saved_signature = state.get('signature')
    empty_notes_migration = not item.get('notes', '').strip() and saved_signature == legacy_signature
    if saved_signature and saved_signature != signature and not empty_notes_migration:
        raise ValueError('Queued idea/settings changed after processing started. Create a new item ID.')
    state['signature'] = signature; save()
    apply_regeneration(item, state, save)
    project = root / 'projects' / item['id']
    if state.get('revision', 1) > 1:
        project = project / f"revision-{state['revision']:03}"
    project.mkdir(parents=True, exist_ok=True)
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'project={project}\n')
    if state.get('status') not in ('ready', 'awaiting_approval', 'publishing', 'uploaded', 'scheduled', 'done'):
        state['status'] = 'generating'; save()
        command = [sys.executable, str(Path(p.__file__)), 'run', '--idea', item['idea'],
                   '--minutes', str(item.get('minutes', 3)), '--out', str(project), '--with-short',
                   '--short-seconds', str(item.get('short_seconds', 55))]
        if item.get('notes', '').strip():
            command.extend(['--notes', item['notes'].strip()])
        subprocess.run(command, check=True)
        metadata(project)
        thumbnail(project, json.loads((project/'metadata.json').read_text())['long'])
        state['status'] = 'ready'; save()
    if queue['email_mode'] == 'review' or state.get('revision', 1) > 1 or os.getenv('REVIEW_ONLY') == '1':
        digest = review_digest(item, project)
        if state.get('review_digest') != digest:
            if state.get('uploads'):
                raise ValueError('Reviewed files changed after upload began; reconcile in YouTube Studio first.')
            if state.get('email_review') == 'sending':
                raise RuntimeError('Previous review email delivery is uncertain; reconcile before changing the package.')
            state['review_digest'] = digest
            state.pop('approved_digest', None); state.pop('email_review', None); save()
        email_summary(item, project, state, save, 'review')
        if os.getenv('REVIEW_ONLY') == '1':
            state['status'] = 'awaiting_approval'; save()
            print('Review-only run complete. YouTube uploads are disabled for this run.')
            return
        approval_code = os.getenv('APPROVAL_CODE', '')
        if approval_code and os.getenv('REVIEW_ACTION', 'approve') == 'approve':
            if approval_code != digest or state.get('email_review') != 'sent':
                raise ValueError('Approval does not match the current emailed package. Use the code from its latest review email.')
            state['approved_digest'] = digest; save()
        if state.get('approved_digest') != digest:
            state['status'] = 'awaiting_approval'; save()
            print('Prepared package; waiting for approval through the emailed workflow instructions. No YouTube uploads sent.')
            return
    if state.get('status') not in ('uploaded', 'scheduled'):
        state.update(status='publishing'); save()
        # Compatibility recovery for versions that saved `started` before a
        # definitive HTTP RuntimeError but did not clear it afterward. Network-level
        # ambiguity uses a different exception type and remains blocked for safety.
        if state.get('last_error_type') == 'RuntimeError':
            recovered = False
            for upload in state.get('uploads', {}).values():
                if upload.get('started') and not upload.get('session') and not upload.get('id'):
                    upload.pop('started', None); upload.pop('signature', None); recovered = True
            if recovered:
                save()
        yt = YouTube(); meta = json.loads((project/'metadata.json').read_text())
        for kind, location in [('long', project), ('short', project/'short')]:
            upload = state.setdefault('uploads', {}).setdefault(kind, {})
            snippet = {k: meta[kind][k] for k in ('title', 'description', 'tags')}
            snippet['categoryId'] = str(queue.get('category_id', '27'))
            video_id = yt.upload(location/'video.mp4', snippet, upload, save,
                made_for_kids=item['made_for_kids'], synthetic=item['contains_synthetic_media'])
            if not upload.get('defaults_set'):
                yt.configure_defaults(video_id, snippet, made_for_kids=item['made_for_kids'],
                                      synthetic=item['contains_synthetic_media'])
                upload['defaults_set'] = True; save()
            if kind == 'long' and not upload.get('thumbnail_set') and not state.get('thumbnail_error'):
                try:
                    yt.thumbnail(video_id, project/'thumbnail.jpg')
                    upload['thumbnail_set'] = True; save()
                except RuntimeError as exc:
                    state['thumbnail_error'] = str(exc)
                    save()
                    print('Custom thumbnail could not be applied; continuing with private uploads.', flush=True)
        state['status'] = 'uploaded'; save()
    email_summary(item, project, state, save, 'confirmation')
    state['status'] = 'done'; state['completed_at'] = datetime.now(timezone.utc).isoformat(); save()


def main():
    os.umask(0o077)
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', type=Path, default=Path('video_queue.json'))
    parser.add_argument('--work-root', type=Path, required=True, help='Persistent private storage; never delete between runs')
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    queue = validate_queue(json.loads(args.queue.read_text()))
    os.environ['VIDEO_TIMEZONE'] = queue.get('timezone', 'America/New_York')
    if args.validate_only:
        print(f"Validated {len(queue['items'])} queued ideas. No external calls."); return
    if not os.getenv('SUMMARY_EMAIL_TO'):
        os.environ['SUMMARY_EMAIL_TO'] = queue.get('notification_email', '')
    root = args.work_root.resolve(); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with ExitStack() as stack:
        cloud = None
        if os.getenv('VIDEO_CLOUD_CHECKPOINTS'):
            from automation.cloud_storage import CloudSession
            cloud = stack.enter_context(CloudSession())
        lock = stack.enter_context((root/'queue.lock').open('w'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root/'state.json'
        states = json.loads(path.read_text()) if path.exists() else {}
        def save():
            p.save_json(path, states)
            os.chmod(path, 0o600)
        if cloud:
            cloud.cleanup(states, save)
        item = choose_item(queue, states)
        if not item:
            print('Queue empty; nothing to do.'); return
        if os.getenv('REVIEW_ACTION', 'approve') not in ('approve', 'regenerate'):
            raise ValueError('Unknown review action.')
        required = ['OPENAI_API_KEY', 'RUNPOD_API_KEY', 'RUNPOD_ENDPOINT_ID', 'RUNPOD_REFERENCE_AUDIO',
                    'SMTP_HOST', 'SMTP_FROM', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SUMMARY_EMAIL_TO']
        if queue['email_mode'] == 'confirmation' or (os.getenv('APPROVAL_CODE') and os.getenv('REVIEW_ACTION', 'approve') == 'approve') or states.get(item['id'], {}).get('approved_digest'):
            required += ['YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'YOUTUBE_REFRESH_TOKEN']
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError('Missing configuration: ' + ', '.join(missing))
        if os.getenv('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(f"project={root / 'projects' / item['id']}\n")
        try:
            process(item, queue, root, states, save)
        except Exception as exc:
            states.setdefault(item['id'], {})['last_error_type'] = type(exc).__name__; save()
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Request exceptions can include resumable session credentials in their URLs.
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Check configuration or service status; saved progress is retained.'
        print(f'Automation stopped ({type(exc).__name__}): {detail}', file=sys.stderr)
        sys.exit(1)
