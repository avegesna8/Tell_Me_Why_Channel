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
import shutil
import smtplib
import ssl
import subprocess
import sys
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import pipeline as p
from automation.production import metadata, social_covers, thumbnail
from automation.youtube import YouTube
from automation.meta import Meta


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
                 and state.get(i['id'], {}).get('status') not in ('done', 'discarded')), None)


def review_digest(item, project):
    files = ['video.mp4', 'short/video.mp4', 'metadata.json', 'thumbnail.jpg',
             'short/covers/facebook.jpg', 'short/covers/instagram.jpg', 'short/covers/tiktok.jpg',
             'script.txt', 'short/script.txt', 'script-quality.json']
    files += [name for name in ('speech-text.txt', 'speech-preparation.json', 'production-review.json',
                                     'short/production-review.json') if (project / name).exists()]
    hashes = {}
    for name in files:
        with (project / name).open('rb') as stream:
            hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return p.fingerprint({'version_folder': project.name, 'files': hashes, 'item': {k: item.get(k) for k in
        # Retain legacy empty schedule keys so already-approved packages keep their digest.
        ('id', 'publish_at', 'short_publish_at', 'made_for_kids', 'contains_synthetic_media')}})


def platform_metadata(value):
    if value.get('platforms'):
        return value['platforms']
    # Backward compatibility for packages generated before platform-specific copy.
    tags = [re.sub(r'[^A-Za-z0-9]', '', tag) for tag in value['short'].get('tags', [])]
    tags = [tag for tag in tags if tag]
    cover = ' '.join(value['short']['title'].replace('?', '').split()[:4])
    social = {
        'caption': value['short']['description'],
        'hashtags': tags,
        'cover_text': cover,
    }
    return {
        'facebook_long': {
            'title': value['long']['title'],
            'description': value['long']['description'],
        },
        'facebook_reel': {**social, 'hashtags': tags[:5]},
        'tiktok': {**social, 'hashtags': tags[:6]},
        'instagram': {**social, 'hashtags': tags[:8]},
    }


def social_description(post):
    hashtags = ' '.join('#' + re.sub(r'[^A-Za-z0-9]', '', tag)
                        for tag in post.get('hashtags', []) if tag)
    return post['caption'] + (('\n\n' + hashtags) if hashtags else '')

def email_summary(item, project, state, save, kind):
    key = 'email_' + kind
    if state.get(key) == 'sent':
        return
    if state.get(key) == 'sending':
        raise RuntimeError('Previous email delivery outcome is unknown. Check inbox before changing its state; no duplicate email sent.')
    message = EmailMessage()
    message['From'] = os.environ['SMTP_FROM']; message['To'] = os.environ['SUMMARY_EMAIL_TO']
    subject_revision = (f" — revision {state.get('revision', 1)}, audio {state.get('audio_revision', 1)}, "
                        f"thumbnail {state.get('thumbnail_revision', 1)}")
    message['Subject'] = f"Video {kind}: {item['id']}{subject_revision}"
    message['Message-ID'] = (f"<{item['id']}.v{state.get('revision', 1)}.a{state.get('audio_revision', 1)}."
                             f"t{state.get('thumbnail_revision', 1)}.{kind}@video-automation.local>")
    generated_metadata = json.loads((project/'metadata.json').read_text())
    platforms = state.get('platform_uploads', {})
    facebook = platforms.get('facebook', {})
    publishing_metadata = platform_metadata(generated_metadata)
    report = {
        'idea': item['idea'],
        'metadata': generated_metadata,
        'youtube': {k: {'url': 'https://www.youtube.com/watch?v=' + value['id'],
                        'privacy_status': 'private'}
                    for k, value in state.get('uploads', {}).items() if value.get('id')},
        'facebook': {
            'long': {'id': facebook.get('long', {}).get('id'), 'status': 'unpublished',
                     'thumbnail': 'thumbnail.jpg', 'metadata': publishing_metadata['facebook_long']},
            'short': {'id': facebook.get('short', {}).get('id'), 'status': 'reel_draft',
                      'cover_file': 'short/covers/facebook.jpg',
                      'cover_step': 'Select the attached Facebook cover while reviewing the Reel draft.',
                      'metadata': publishing_metadata['facebook_reel']},
            'manage_url': ('https://www.facebook.com/professional_dashboard/content/'
                           'content_library/?filter=DRAFT' if facebook else None),
        },
        'tiktok': {
            'short': {
                'status': 'manual_upload_required',
                'studio_url': 'https://www.tiktok.com/tiktokstudio/upload',
                'video_file': 'short-video.mp4',
                'cover_file': 'short-cover-tiktok.jpg',
                'caption_and_hashtags': social_description(publishing_metadata['tiktok']),
                'metadata': publishing_metadata['tiktok'],
                'ai_generated_content': item['contains_synthetic_media'],
                'steps': [
                    'Open TikTok Studio while signed into the Tell Me Why account.',
                    'Upload short-video.mp4 (or download short/video.mp4 from the GitHub artifact).',
                    'Paste caption_and_hashtags, select the attached cover, and set the appropriate AI-content disclosure.',
                    'Review visibility and interaction settings, then publish or schedule manually.',
                ],
            }
        },
        'instagram': {
            'status': ('published' if platforms.get('instagram', {}).get('short', {}).get('id') else 'not_published'),
            'id': platforms.get('instagram', {}).get('short', {}).get('id'),
            'url': 'https://www.instagram.com/tellmewhyexplained/?hl=en',
            'account_id': os.getenv('META_INSTAGRAM_ACCOUNT_ID'),
            'video_file': 'short/video.mp4',
            'cover_file': 'short/covers/instagram.jpg',
            'metadata': publishing_metadata['instagram'],
            'cover_behavior': 'Instagram uses the video frame at 1.0 seconds; its publishing API cannot use the attached local cover without a public cover URL.',
            'next_step': ('The approved Reel is already live; use Instagram for later edits.'
                          if platforms.get('instagram', {}).get('short', {}).get('id') else
                          'Approving this package publishes the Reel directly to Instagram.'),
        },
        'workflow_run': ('https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' +
                         os.environ['GITHUB_RUN_ID']) if os.getenv('GITHUB_RUN_ID') else None,
    }
    report['soundtracks'] = {kind: json.loads((location/'soundtrack.json').read_text())
                            for kind, location in [('long',project),('short',project/'short')]
                            if (location/'soundtrack.json').exists()}
    if (project/'script-quality.json').exists():
        report['script_quality'] = json.loads((project/'script-quality.json').read_text())
    if state.get('thumbnail_error'):
        report['thumbnail_warning'] = state['thumbnail_error']
    if state.get('regeneration_comments'):
        report['regeneration_comments'] = state['regeneration_comments']
    if state.get('thumbnail_comments'):
        report['thumbnail_comments'] = state['thumbnail_comments']
    if state.get('audio_regeneration_comments'):
        report['audio_regeneration_comments'] = state['audio_regeneration_comments']
    for selected_kind in ('long', 'short'):
        for key in (f'{selected_kind}_regeneration_comments', f'{selected_kind}_audio_regeneration_comments'):
            if state.get(key):
                report[key] = state[key]
    approval_url = ('https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/workflows/video-queue.yml') if os.getenv('GITHUB_REPOSITORY') else None
    if kind == 'review':
        report['approval'] = {'workflow': approval_url, 'approval_code': state['review_digest'],
                              'revision': state.get('revision', 1), 'audio_revision': state.get('audio_revision', 1),
                              'actions': ['approve', 'regenerate', 'regenerate_long', 'regenerate_short', 'regenerate_audio', 'regenerate_long_audio', 'regenerate_short_audio', 'regenerate_thumbnail', 'delete'],
                              'comments': 'Optional for any regeneration action'}
    p.save_json(project / 'upload-details.json', report)
    long_video = project/'video.mp4'
    short_video = project/'short/video.mp4'
    videos = [long_video, short_video]
    if sum(file.stat().st_size for file in videos) <= 14 * 1024 * 1024:
        attached_videos = videos
        video_note = 'Both videos are attached. Use short-video.mp4 for TikTok Studio.'
    elif short_video.stat().st_size <= 12 * 1024 * 1024:
        attached_videos = [short_video]
        video_note = ('The TikTok Short is attached as short-video.mp4. Download the long video from the '
                      'video-review artifact on the workflow-run link below.')
    else:
        attached_videos = []
        video_note = ('Download short/video.mp4 and the long video from the video-review artifact on the workflow-run '
                      'link below. The artifact appears after this workflow finishes; sign in to GitHub to access it.')
    message.set_content(video_note + '\n\n' + json.dumps(report, indent=2) +
        ('\n\nOpen the workflow link, click Run workflow, paste the approval code above, then choose review_action: '
         'approve to upload YouTube privately, create Facebook drafts, and publish the Instagram Reel live; TikTok remains a manual Studio upload using the attached package; '
         'regenerate to create a completely new version, '
         'regenerate_long to replace only the long video package, regenerate_short to replace only the Short, '
         'regenerate_audio to rebuild audio for both videos, regenerate_long_audio or regenerate_short_audio to rebuild audio for only one format, '
         'regenerate_thumbnail to replace only the thumbnail, or delete to permanently discard the package without uploading. '
         'Optional review_comments document the requested change. Regenerate recreates the script, both videos, images, thumbnail and metadata, '
         'and sends a new review email. Previous regenerated versions are preserved until delete is chosen. '
         'Replies to this email are not processed.' if kind == 'review' else ''))
    for file in [project/'upload-details.json', project/'thumbnail.jpg',
                 project/'short/covers/facebook.jpg', project/'short/covers/instagram.jpg',
                 project/'short/covers/tiktok.jpg', project/'script.txt', project/'short/script.txt',
                 project/'script-quality.json', project/'speech-text.txt', project/'speech-preparation.json',
                 project/'production-review.json', project/'short/production-review.json',
                 project/'narration-quality.json', project/'short/narration-quality.json',
                 project/'image-quality.json', project/'short/image-quality.json']:
        if not file.exists():
            continue
        mime = ('image', 'jpeg') if file.suffix == '.jpg' else ('application', 'json') if file.suffix == '.json' else ('text', 'plain')
        attachment_name = (f'short-cover-{file.name}' if file.parent.name == 'covers' else
                           ('short-' if file.parent.name == 'short' else '') + file.name)
        message.add_attachment(file.read_bytes(), maintype=mime[0], subtype=mime[1],
                               filename=attachment_name)
    for file in attached_videos:
        message.add_attachment(file.read_bytes(), maintype='video', subtype='mp4',
                               filename='short-video.mp4' if file.parent.name == 'short' else 'long-video.mp4')
    # Establish/authenticate before recording an ambiguous send attempt.
    with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT', '587')), timeout=60) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(os.environ['SMTP_USERNAME'], os.environ['SMTP_PASSWORD'])
        state[key] = 'sending'; save()
        client.send_message(message)
        state[key] = 'sent'; save()


def apply_deletion(item, state, root, save, cloud=None):
    if os.getenv('REVIEW_ACTION', 'approve') != 'delete':
        return False
    code = os.getenv('APPROVAL_CODE', '')
    if not code:
        raise ValueError('Deletion requires the code from its review email.')
    if state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError('Delete is available only for a generated package awaiting review.')
    if (any(upload.get('id') or upload.get('session') or upload.get('started')
            for upload in state.get('uploads', {}).values()) or state.get('platform_uploads')):
        raise ValueError('Delete cannot remove a package after any platform upload began; reconcile the platform dashboards.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError('Deletion code is stale or has not been emailed.')
    signature, revision = state.get('signature'), state.get('revision', 1)
    if cloud:
        cloud.discard_project(item['id'])
    else:
        shutil.rmtree(Path(root) / 'projects' / item['id'], ignore_errors=True)
    state.clear()
    state.update(status='discarded', discarded_at=datetime.now(timezone.utc).isoformat(),
                 signature=signature, revision=revision)
    save()
    print(f"Discarded {item['id']}; no platform uploads were sent.", flush=True)
    return True


def apply_regeneration(item, state, save):
    if os.getenv('REVIEW_ACTION', 'approve') != 'regenerate':
        return
    code = os.getenv('APPROVAL_CODE', '')
    if not code:
        raise ValueError('Regeneration requires the code from its review email.')
    if code in state.get('regeneration_requests', []):
        comments = os.getenv('REVIEW_COMMENTS', '').strip()
        if comments != state.get('regeneration_comments', ''):
            raise ValueError('That approval code was already used with different regeneration comments. Use the latest review email code.')
        return  # Retried dispatch resumes that version, not another paid generation.
    if state.get('uploads') or state.get('platform_uploads') or state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError('Regenerate is available before uploading. Reconcile existing platform uploads first.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError('Regeneration code is stale or has not been emailed.')
    history = state.get('history', []) + [{k: v for k, v in state.items() if k != 'history'}]
    revision = state.get('revision', 1) + 1
    requests = state.get('regeneration_requests', []) + [code]
    signature = state.get('signature')
    comments = os.getenv('REVIEW_COMMENTS', '').strip()
    if len(comments) > 2000:
        raise ValueError('Review comments must be 2,000 characters or fewer.')
    state.clear()
    state.update(revision=revision, status='generating', history=history,
                 regeneration_requests=requests, signature=signature, regeneration_comments=comments)
    save()


def clear_selected_output(project, kind, cloud=None):
    """Idempotently remove one output locally and from durable checkpoints."""
    if cloud:
        cloud.discard_output(project, kind)
    if kind == 'short':
        shutil.rmtree(project/'short', ignore_errors=True)
    else:
        for path in list(project.iterdir()):
            if path.name not in ('short', 'thumbnail-history'):
                shutil.rmtree(path) if path.is_dir() else path.unlink()


def apply_selected_regeneration(item, state, project, save, cloud=None):
    action = os.getenv('REVIEW_ACTION', 'approve')
    if action not in ('regenerate_long', 'regenerate_short'):
        return
    kind = action.removeprefix('regenerate_')
    code = os.getenv('APPROVAL_CODE', '')
    comments = os.getenv('REVIEW_COMMENTS', '').strip()
    request_key = f'{kind}_regeneration_requests'
    comment_key = f'{kind}_regeneration_comments'
    if not code:
        raise ValueError(f'{kind.title()} regeneration requires the code from its review email.')
    cleanup_key = f'{kind}_regeneration_cleanup_done'
    if code in state.get(request_key, []):
        if comments != state.get(comment_key, ''):
            raise ValueError(f'That approval code already regenerated the {kind} video with different comments. Use the latest review email code.')
        if state.get('selected_regeneration_active') == kind:
            if state.get(cleanup_key) is False:
                clear_selected_output(project, kind, cloud)
            if not state.get(cleanup_key):
                # Legacy active requests already ran cleanup before recording this checkpoint.
                state[cleanup_key] = True; save()
        return
    if state.get('uploads') or state.get('platform_uploads') or state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError(f'{kind.title()} regeneration is available only before any platform upload begins.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError(f'{kind.title()} regeneration code is stale or has not been emailed.')
    if len(comments) > 2000:
        raise ValueError('Review comments must be 2,000 characters or fewer.')
    required = ([project/'short/video.mp4', project/'short/script.txt', project/'short/storyboard.json']
                if kind == 'long' else
                [project/'video.mp4', project/'script.txt', project/'storyboard.json', project/'thumbnail.jpg'])
    if not all(path.exists() for path in required):
        raise ValueError(f'The reviewed package is incomplete and cannot regenerate only the {kind} video.')
    # Record intent first so interruption during deletion resumes with the same consumed code.
    state[request_key] = state.get(request_key, []) + [code]
    state[comment_key] = comments
    state['selected_regeneration_active'] = kind
    state[cleanup_key] = False
    state['status'] = 'generating'
    state.pop('approved_digest', None); state.pop('review_digest', None); state.pop('email_review', None)
    save()
    clear_selected_output(project, kind, cloud)
    state[cleanup_key] = True; save()


def clear_audio_outputs(project, scope="both"):
    """Remove selected speech/render outputs while preserving scripts and all imagery."""
    project = Path(project)
    if scope not in ('both', 'long', 'short'):
        raise ValueError('Audio cleanup scope must be both, long, or short.')
    exact_names = {
        'video.mp4', 'video.partial.mp4', 'video.narration.mp4', 'soundtrack.partial.mp4',
        'soundtrack-mix.json', 'soundtrack.json', 'captions.srt', 'captions.ass',
        'narration-quality.json', 'transcript.json', 'short-timing.json',
    }
    for path in list(project.rglob('*')):
        if not path.is_file():
            continue
        in_short = path.relative_to(project).parts[0] == 'short'
        if (scope == 'long' and in_short) or (scope == 'short' and not in_short):
            continue
        audio_asset = (path.parent.name == 'assets' and
                       (path.suffix.lower() in ('.wav', '.mp3') or
                        path.name.endswith(('.audio.json', '.runpod-job.json')) or
                        path.name.startswith('narration-quality')))
        if path.name in exact_names or audio_asset:
            path.unlink()


def apply_audio_regeneration(item, state, project, save):
    if os.getenv('REVIEW_ACTION', 'approve') != 'regenerate_audio':
        return
    code = os.getenv('APPROVAL_CODE', '')
    if not code:
        raise ValueError('Audio regeneration requires the code from its review email.')
    comments = os.getenv('REVIEW_COMMENTS', '').strip()
    if code in state.get('audio_regeneration_requests', []):
        if comments != state.get('audio_regeneration_comments', ''):
            raise ValueError('That approval code already regenerated audio with different comments. Use the latest review email code.')
        return
    if state.get('uploads') or state.get('platform_uploads') or state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError('Audio regeneration is available only before any platform upload begins.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError('Audio regeneration code is stale or has not been emailed.')
    if len(comments) > 2000:
        raise ValueError('Review comments must be 2,000 characters or fewer.')
    required = [project/'script.txt', project/'storyboard.json', project/'metadata.json',
                project/'thumbnail.jpg', project/'short/script.txt', project/'short/storyboard.json']
    if not all(path.exists() for path in required):
        raise ValueError('The reviewed package is incomplete and cannot use audio-only regeneration.')
    clear_audio_outputs(project)
    state['audio_regeneration_requests'] = state.get('audio_regeneration_requests', []) + [code]
    state['audio_revision'] = state.get('audio_revision', 1) + 1
    state['audio_regeneration_comments'] = comments
    state['audio_regeneration_active'] = True
    state['status'] = 'generating'
    state.pop('approved_digest', None); state.pop('review_digest', None); state.pop('email_review', None)
    save()


def apply_selected_audio_regeneration(item, state, project, save, cloud=None):
    action = os.getenv('REVIEW_ACTION', 'approve')
    if action not in ('regenerate_long_audio', 'regenerate_short_audio'):
        return
    kind = 'long' if action == 'regenerate_long_audio' else 'short'
    code = os.getenv('APPROVAL_CODE', '')
    comments = os.getenv('REVIEW_COMMENTS', '').strip()
    request_key = f'{kind}_audio_regeneration_requests'
    comment_key = f'{kind}_audio_regeneration_comments'
    if not code:
        raise ValueError(f'{kind.title()} audio regeneration requires the code from its review email.')
    cleanup_key = f'{kind}_audio_regeneration_cleanup_done'
    if code in state.get(request_key, []):
        if comments != state.get(comment_key, ''):
            raise ValueError(f'That approval code already regenerated {kind} audio with different comments. Use the latest review email code.')
        if state.get('selected_audio_regeneration_active') == kind:
            if state.get(cleanup_key) is False:
                if cloud:
                    cloud.discard_audio_output(project, kind)
                clear_audio_outputs(project, scope=kind)
            if not state.get(cleanup_key):
                state[cleanup_key] = True; save()
        return
    if state.get('uploads') or state.get('platform_uploads') or state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError(f'{kind.title()} audio regeneration is available only before any platform upload begins.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError(f'{kind.title()} audio regeneration code is stale or has not been emailed.')
    if len(comments) > 2000:
        raise ValueError('Review comments must be 2,000 characters or fewer.')
    required = ([project/'script.txt', project/'storyboard.json', project/'assets'] if kind == 'long' else
                [project/'short/script.txt', project/'short/storyboard.json', project/'short/assets'])
    if not all(path.exists() for path in required):
        raise ValueError(f'The reviewed package is incomplete and cannot regenerate only {kind} audio.')
    state[request_key] = state.get(request_key, []) + [code]
    state[comment_key] = comments
    state['selected_audio_regeneration_active'] = kind
    state[cleanup_key] = False
    state['audio_regeneration_active'] = True
    state['status'] = 'generating'
    state.pop('approved_digest', None); state.pop('review_digest', None); state.pop('email_review', None)
    save()
    if cloud:
        cloud.discard_audio_output(project, kind)
    clear_audio_outputs(project, scope=kind)
    state[cleanup_key] = True; save()


def apply_thumbnail_regeneration(item, state, project, save):
    if os.getenv('REVIEW_ACTION', 'approve') != 'regenerate_thumbnail':
        return
    code = os.getenv('APPROVAL_CODE', '')
    if not code:
        raise ValueError('Thumbnail regeneration requires the code from its review email.')
    if code in state.get('thumbnail_regeneration_requests', []):
        comments = os.getenv('REVIEW_COMMENTS', '').strip()
        if comments != state.get('thumbnail_comments', ''):
            raise ValueError('That approval code already generated a thumbnail with different comments. Use the latest review email code.')
        return
    if state.get('uploads') or state.get('platform_uploads') or state.get('status') not in ('ready', 'awaiting_approval'):
        raise ValueError('Thumbnail regeneration is available only before any platform upload begins.')
    if code != state.get('review_digest') or state.get('email_review') != 'sent':
        raise ValueError('Thumbnail regeneration code is stale or has not been emailed.')
    comments = os.getenv('REVIEW_COMMENTS', '').strip()
    if len(comments) > 2000:
        raise ValueError('Review comments must be 2,000 characters or fewer.')
    if not (project / 'metadata.json').exists():
        raise ValueError('The reviewed package has no metadata for thumbnail regeneration.')
    old_thumbnail = project / 'thumbnail.jpg'
    if old_thumbnail.exists():
        history = project / 'thumbnail-history'; history.mkdir(exist_ok=True)
        shutil.copy2(old_thumbnail, history / f"thumbnail-{code[:12]}.jpg")
    thumbnail(project, json.loads((project/'metadata.json').read_text())['long'], comments=comments)
    state['thumbnail_regeneration_requests'] = state.get('thumbnail_regeneration_requests', []) + [code]
    state['thumbnail_revision'] = state.get('thumbnail_revision', 1) + 1
    state['thumbnail_comments'] = comments
    state['status'] = 'ready'
    state.pop('approved_digest', None); state.pop('review_digest', None)
    state.pop('email_review', None); state.pop('thumbnail_error', None)
    save()


def process(item, queue, root, states, save, cloud=None):
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
    if apply_deletion(item, state, root, save, cloud):
        return
    apply_regeneration(item, state, save)
    project = root / 'projects' / item['id']
    if state.get('revision', 1) > 1:
        project = project / f"revision-{state['revision']:03}"
    project.mkdir(parents=True, exist_ok=True)
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'project={project}\n')
    apply_selected_regeneration(item, state, project, save, cloud)
    apply_audio_regeneration(item, state, project, save)
    apply_selected_audio_regeneration(item, state, project, save, cloud)
    apply_thumbnail_regeneration(item, state, project, save)
    if state.get('status') not in ('ready', 'awaiting_approval', 'publishing', 'uploaded', 'scheduled', 'done'):
        state['status'] = 'generating'; save()
        selected = state.get('selected_regeneration_active')
        selected_audio = state.get('selected_audio_regeneration_active')
        notes = item.get('notes', '').strip()
        if selected == 'short' or selected_audio == 'short':
            command = [sys.executable, str(Path(p.__file__)), 'short', '--out', str(project),
                       '--short-seconds', str(item.get('short_seconds', 55))]
            notes = state.get('short_regeneration_comments', '').strip() if selected == 'short' else ''
        else:
            command = [sys.executable, str(Path(p.__file__)), 'run']
            if state.get('audio_regeneration_active'):
                command.extend(['--script', str(project/'script.txt')])
            else:
                command.extend(['--idea', item['idea'], '--minutes', str(item.get('minutes', 3))])
            command.extend(['--out', str(project)])
            if selected != 'long' and selected_audio != 'long':
                command.extend(['--with-short', '--short-seconds', str(item.get('short_seconds', 55))])
            comments = (state.get('long_regeneration_comments', '') if selected == 'long'
                        else state.get('regeneration_comments', ''))
            if comments.strip() and not state.get('audio_regeneration_active'):
                notes = (notes + '\n\nAdditional regeneration instructions:\n' + comments.strip()).strip()
        if notes:
            command.extend(['--notes', notes])
        run_env = None
        if state.get('audio_regeneration_active') or selected:
            # A fresh selected regeneration has no plan; a resumed one safely retimes its saved plan.
            run_env = {**os.environ, 'REUSE_SOUNDTRACK_PLAN': '1'}
        subprocess.run(command, check=True, env=run_env)
        if selected == 'long':
            metadata(project, replace_kind='long')
            thumbnail(project, json.loads((project/'metadata.json').read_text())['long'],
                      comments=state.get('long_regeneration_comments', ''))
            social_covers(project, platform_metadata(json.loads((project/'metadata.json').read_text())))
        elif selected == 'short':
            metadata(project, replace_kind='short')
            social_covers(project, platform_metadata(json.loads((project/'metadata.json').read_text())))
        elif not state.get('audio_regeneration_active'):
            metadata(project)
            thumbnail(project, json.loads((project/'metadata.json').read_text())['long'],
                      comments=state.get('regeneration_comments', ''))
            generated = json.loads((project/'metadata.json').read_text())
            social_covers(project, platform_metadata(generated))
        state.pop('audio_regeneration_active', None)
        completed_selected = state.pop('selected_regeneration_active', None)
        completed_audio = state.pop('selected_audio_regeneration_active', None)
        if completed_selected:
            state.pop(f'{completed_selected}_regeneration_cleanup_done', None)
        if completed_audio:
            state.pop(f'{completed_audio}_audio_regeneration_cleanup_done', None)
        state['status'] = 'ready'; save()
    cover_paths = [project/'short/covers'/f'{name}.jpg' for name in ('facebook', 'instagram', 'tiktok')]
    if (project/'short/assets/scene-001.png').exists() and not all(path.exists() for path in cover_paths):
        generated = json.loads((project/'metadata.json').read_text())
        social_covers(project, platform_metadata(generated))
        save()
    if queue['email_mode'] == 'review' or state.get('revision', 1) > 1 or os.getenv('REVIEW_ONLY') == '1':
        digest = review_digest(item, project)
        if state.get('review_digest') != digest:
            if state.get('uploads'):
                raise ValueError('Reviewed files changed after upload began; reconcile the platform dashboards first.')
            if state.get('email_review') == 'sending':
                raise RuntimeError('Previous review email delivery is uncertain; reconcile before changing the package.')
            state['review_digest'] = digest
            state.pop('approved_digest', None); state.pop('email_review', None); save()
        email_summary(item, project, state, save, 'review')
        if os.getenv('REVIEW_ONLY') == '1':
            state['status'] = 'awaiting_approval'; save()
            print('Review-only run complete. All platform uploads are disabled for this run.')
            return
        approval_code = os.getenv('APPROVAL_CODE', '')
        if approval_code and os.getenv('REVIEW_ACTION', 'approve') == 'approve':
            if approval_code != digest or state.get('email_review') != 'sent':
                raise ValueError('Approval does not match the current emailed package. Use the code from its latest review email.')
            state['approved_digest'] = digest; save()
        if state.get('approved_digest') != digest:
            state['status'] = 'awaiting_approval'; save()
            print('Prepared package; waiting for approval through the emailed workflow instructions. No platform uploads sent.')
            return
    legacy_uploaded = state.get('status') in ('uploaded', 'scheduled')
    youtube_complete = legacy_uploaded or all(
        state.get('uploads', {}).get(kind, {}).get('id') for kind in ('long', 'short')
    )
    facebook_state = state.get('platform_uploads', {}).get('facebook', {})
    facebook_complete = legacy_uploaded or (
        facebook_state.get('long', {}).get('id') and facebook_state.get('short', {}).get('finished')
    )
    instagram_state = state.get('platform_uploads', {}).get('instagram', {}).get('short', {})
    instagram_complete = legacy_uploaded or instagram_state.get('id')
    tiktok_state = state.setdefault('platform_uploads', {}).setdefault('tiktok', {}).setdefault('short', {})
    if not tiktok_state.get('skipped'):
        tiktok_state.update(skipped=True, delivery_status='manual_upload_required')
        for key in ('started', 'signature', 'publish_id', 'upload_url', 'upload_started', 'uploaded'):
            tiktok_state.pop(key, None)
        save()
    if not (youtube_complete and facebook_complete and instagram_complete):
        meta = json.loads((project/'metadata.json').read_text())
        social_meta = platform_metadata(meta)
        state.update(status='publishing')
        save()
        # Recover only definitive YouTube initiation failures. Network ambiguity
        # stays blocked so a retry cannot create a duplicate.
        if state.get('last_error_type') == 'RuntimeError':
            recovered = False
            for upload in state.get('uploads', {}).values():
                if upload.get('started') and not upload.get('session') and not upload.get('id'):
                    upload.pop('started', None)
                    upload.pop('signature', None)
                    recovered = True
            if recovered:
                save()

        if not youtube_complete:
            yt = YouTube()
            for video_kind, location in [('long', project), ('short', project/'short')]:
                upload = state.setdefault('uploads', {}).setdefault(video_kind, {})
                snippet = {key: meta[video_kind][key] for key in ('title', 'description', 'tags')}
                snippet['categoryId'] = str(queue.get('category_id', '27'))
                video_id = yt.upload(
                    location/'video.mp4', snippet, upload, save,
                    made_for_kids=item['made_for_kids'],
                    synthetic=item['contains_synthetic_media'],
                )
                if not upload.get('defaults_set'):
                    yt.configure_defaults(
                        video_id, snippet,
                        made_for_kids=item['made_for_kids'],
                        synthetic=item['contains_synthetic_media'],
                    )
                    upload['defaults_set'] = True
                    save()
                if video_kind == 'long' and not upload.get('thumbnail_set') and not state.get('thumbnail_error'):
                    try:
                        yt.thumbnail(video_id, project/'thumbnail.jpg')
                        upload['thumbnail_set'] = True
                        save()
                    except RuntimeError as exc:
                        state['thumbnail_error'] = str(exc)
                        save()
                        print('Custom thumbnail could not be applied; continuing with private uploads.', flush=True)

        meta_client = None
        if not facebook_complete:
            facebook = state.setdefault('platform_uploads', {}).setdefault('facebook', {})
            meta_client = Meta()
            fb = meta_client
            fb.upload_unpublished_video(
                project/'video.mp4', social_meta['facebook_long'], facebook.setdefault('long', {}), save,
                thumbnail=project/'thumbnail.jpg',
            )
            facebook_reel = {
                'title': social_meta['facebook_reel']['cover_text'],
                'description': social_description(social_meta['facebook_reel']),
            }
            fb.upload_reel_draft(
                project/'short'/'video.mp4', facebook_reel, facebook.setdefault('short', {}), save,
            )

        if not instagram_complete:
            if meta_client is None:
                meta_client = Meta()
            instagram_post = social_meta['instagram']
            meta_client.publish_instagram_reel(
                project/'short'/'video.mp4',
                {'description': social_description(instagram_post)},
                state.setdefault('platform_uploads', {}).setdefault('instagram', {}).setdefault('short', {}),
                save,
            )

        state['status'] = 'uploaded'
        save()
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
        if os.getenv('REVIEW_ACTION', 'approve') not in ('approve', 'regenerate', 'regenerate_long', 'regenerate_short', 'regenerate_audio', 'regenerate_long_audio', 'regenerate_short_audio', 'regenerate_thumbnail', 'delete'):
            raise ValueError('Unknown review action.')
        required = ['OPENAI_API_KEY', 'RUNPOD_API_KEY', 'RUNPOD_ENDPOINT_ID', 'RUNPOD_REFERENCE_AUDIO',
                    'SMTP_HOST', 'SMTP_FROM', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SUMMARY_EMAIL_TO']
        if queue['email_mode'] == 'confirmation' or (os.getenv('APPROVAL_CODE') and os.getenv('REVIEW_ACTION', 'approve') == 'approve') or states.get(item['id'], {}).get('approved_digest'):
            required += [
                'YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'YOUTUBE_REFRESH_TOKEN',
                'META_PAGE_ID', 'META_PAGE_ACCESS_TOKEN',
            ]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ValueError('Missing configuration: ' + ', '.join(missing))
        if os.getenv('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(f"project={root / 'projects' / item['id']}\n")
        try:
            process(item, queue, root, states, save, cloud)
            if states.get(item['id'], {}).get('status') in ('done', 'discarded') and os.getenv('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    output.write(f"completed_item={item['id']}\n")
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
