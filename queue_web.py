"""Local-only queue dashboard: validate, commit, and push video ideas."""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import subprocess
import threading
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from automation.queue_runner import validate_queue

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / 'video_queue.json'
WORKFLOW_URL = 'https://github.com/avegesna8/Tell_Me_Why_Channel/actions/workflows/video-queue.yml'


def validate_idea(value):
    try:
        request_id = str(uuid.UUID(value.get('request_id', '')))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid browser request ID.') from exc
    idea = value.get('idea', '').strip() if isinstance(value.get('idea'), str) else ''
    notes = value.get('notes', '').strip() if isinstance(value.get('notes', ''), str) else ''
    minutes = value.get('minutes')
    short_seconds = value.get('short_seconds')
    if not 3 <= len(idea) <= 500:
        raise ValueError('Video idea must contain 3–500 characters.')
    if len(notes) > 5000:
        raise ValueError('Notes must contain at most 5000 characters.')
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or not .25 <= minutes <= 30:
        raise ValueError('Long video length must be between 0.25 and 30 minutes.')
    if type(short_seconds) is not int or not 30 <= short_seconds <= 60:
        raise ValueError('Short length must be an integer between 30 and 60 seconds.')
    result = {'request_id': request_id, 'idea': idea, 'notes': notes,
              'minutes': minutes, 'short_seconds': short_seconds}
    for key, default in [('enabled', True), ('made_for_kids', False), ('contains_synthetic_media', False)]:
        field = value.get(key, default)
        if not isinstance(field, bool):
            raise ValueError(f'{key} must be true or false.')
        result[key] = field
    return result


def run_git(*args, check=True):
    result = subprocess.run(['git', *args], cwd=ROOT, text=True, capture_output=True, timeout=90)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[-1][:300] if detail else f'git {args[0]} failed')
    return result


def slug(idea, request_id):
    base = re.sub(r'[^a-z0-9]+', '-', idea.lower()).strip('-')[:55] or 'video-idea'
    return f'{base}-{request_id.replace("-", "")[:8]}'


def load_queue():
    return validate_queue(json.loads(QUEUE.read_text()))


def save_queue(value):
    temp = QUEUE.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temp.replace(QUEUE)


def push_queue():
    queue = load_queue()
    branch = run_git('branch', '--show-current').stdout.strip()
    if branch != 'main':
        raise RuntimeError(f'Queue dashboard requires the main branch; current branch is {branch or "detached"}.')
    run_git('add', '--', 'video_queue.json')
    staged = run_git('diff', '--cached', '--quiet', '--', 'video_queue.json', check=False)
    committed = staged.returncode != 0
    if committed:
        run_git('commit', '-m', 'Add video idea to production queue', '--', 'video_queue.json')
    run_git('push', 'origin', 'main')
    return {'ok': True, 'committed': committed, 'count': len(queue['items']),
            'message': 'Queue validated and pushed to GitHub.', 'workflow_url': WORKFLOW_URL}


def add_idea(value):
    payload = validate_idea(value)
    with (ROOT / '.queue-web.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        queue = load_queue()
        item_id = slug(payload['idea'], payload['request_id'])
        existing = next((item for item in queue['items'] if item['id'] == item_id), None)
        if existing is None:
            queue['items'].append({'id': item_id, 'idea': payload['idea'], 'notes': payload['notes'],
                'minutes': payload['minutes'], 'short_seconds': payload['short_seconds'],
                'enabled': payload['enabled'], 'made_for_kids': payload['made_for_kids'],
                'contains_synthetic_media': payload['contains_synthetic_media']})
            validate_queue(queue)
            save_queue(queue)
        result = push_queue()
        result.update(item_id=item_id, already_present=existing is not None)
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'TellMeWhyQueue/1.0'

    def log_message(self, format, *args):
        return

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            body = (ROOT/'queue_ui/index.html').read_bytes()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body); return
        if path == '/api/queue':
            try:
                queue = load_queue()
                changed = run_git('diff', '--quiet', '--', 'video_queue.json', check=False).returncode != 0
                self.send_json(200, {'items': queue['items'], 'has_uncommitted_queue_changes': changed,
                                     'workflow_url': WORKFLOW_URL})
            except Exception as exc:
                self.send_json(500, {'detail': str(exc)})
            return
        self.send_json(404, {'detail': 'Not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 100_000:
                raise ValueError('Request is too large.')
            body = json.loads(self.rfile.read(length)) if length else {}
            if path == '/api/ideas':
                self.send_json(200, add_idea(body)); return
            if path == '/api/push':
                with (ROOT/'.queue-web.lock').open('w') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.send_json(200, push_queue())
                return
            self.send_json(404, {'detail': 'Not found'})
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {'detail': str(exc)})
        except Exception as exc:
            self.send_json(500, {'detail': str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError('Port must be between 1024 and 65535.')
    if not args.no_browser:
        threading.Timer(.8, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Queue dashboard: http://127.0.0.1:{args.port} (press Control-C to stop)')
    server.serve_forever()


if __name__ == '__main__':
    main()
