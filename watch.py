"""Read-only Metaculus gate; dispatch the private monitor only when work is due."""
import io
import json
import os
import subprocess
import zipfile
from urllib.error import HTTPError
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

TERMINAL = {'accepted', 'already_forecasted', 'closed', 'deadline_missed',
            'blocked_integrity', 'provider_blocked', 'platform_rejected'}
MONITOR = 'run_bot_on_tournament.yaml'
WORKER = 'official_competition.yaml'


def now():
    return datetime.now(timezone.utc)


def date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def gh(path, method='GET', body=None, binary=False):
    args = ['gh', 'api', path, '--method', method]
    if body is not None:
        args += ['--input', '-']
    result = subprocess.run(args, input=json.dumps(body).encode() if body is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Never expose private run metadata, response bodies or secrets in public logs.
        raise RuntimeError('GitHub request failed; check credentials and target Actions permission')
    return result.stdout if binary else json.loads(result.stdout or b'{}')


def pages(path, field):
    out = []
    for page in range(1, 101):
        data = gh(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        rows = data[field]
        out.extend(rows)
        if len(rows) < 100:
            return out
    raise RuntimeError('GitHub pagination incomplete')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def open_ids(token, get=None):
    url = 'https://www.metaculus.com/api/posts/?' + urlencode({
        'tournaments': 33121, 'statuses': 'open', 'limit': 100})
    ids = set()
    visited = set()
    while url:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != 'www.metaculus.com' or parsed.path != '/api/posts/':
            raise ValueError('Unapproved Metaculus pagination URL')
        if url in visited or len(visited) >= 100:
            raise ValueError('Metaculus pagination incomplete')
        visited.add(url)
        if get:
            data = get(url)
        else:
            request = Request(url, headers={'Authorization': 'Token ' + token,
                'Accept': 'application/json', 'User-Agent': 'FutureEval-public-monitor/1.0'})
            with build_opener(NoRedirect()).open(request, timeout=45) as response:
                data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError('Unexpected Metaculus response')
        rows = data.get('results', data.get('posts'))
        if not isinstance(rows, list):
            raise ValueError('Question list missing')
        for post in rows:
            if post.get('title', '').startswith('[PRACTICE]'):
                continue
            qs = ([post['question']] if post.get('question') else []) + (post.get('group_of_questions') or {}).get('questions', [])
            for question in qs:
                if question.get('status') != 'open':
                    continue
                deadlines = [date(question[k]) for k in ('actual_close_time', 'close_time',
                    'scheduled_close_time', 'spot_scoring_time') if question.get(k)]
                if deadlines and min(deadlines) <= now():
                    continue
                ident = str(question['id'])
                if not ident.isdecimal():
                    raise ValueError('Invalid question identity')
                ids.add(ident)
        url = data.get('next')
        if url and url.startswith('/'):
            url = 'https://www.metaculus.com' + url
    return ids


def decision(ids, state, *, active=False, heartbeat=False):
    if active:
        return 'worker_active'
    tasks = state.get('tasks', {})
    if ids - tasks.keys():
        return 'new_questions'
    if any(t.get('stage') not in TERMINAL and
           (not t.get('retry_at_utc') or date(t['retry_at_utc']) <= now()) for t in tasks.values()):
        return 'pending_due'
    return 'checkpoint_refresh' if heartbeat else 'idle'


def run():
    target = os.environ['TARGET_REPO']
    if target != 'chenencc/futureeval-forecasting-agent':
        raise ValueError('Unexpected dispatch target')
    token = os.environ.get('METACULUS_TOKEN')
    if not token:
        raise ValueError('Configure METACULUS_TOKEN as a repository secret')
    token = token.strip()
    if token.startswith('Token '):
        token = token[6:].strip()
    base = f'repos/{target}/actions'
    recent = []
    for workflow in (MONITOR, WORKER):
        recent.extend(gh(f'{base}/workflows/{workflow}/runs?per_page=30')['workflow_runs'])
    active = any(r['status'] != 'completed' or (now() - date(r['created_at'])).total_seconds() < 120 for r in recent)
    ids = open_ids(token)
    if active:
        return {'schema': 'public-monitor-health-v1', 'checked_at_utc': now().isoformat(),
            'reason': 'worker_active', 'dispatched': False,
            'open_question_count': len(ids), 'open_scan_complete': True}
    state = {'tasks': {}}
    checkpoint_time = None
    completed = sorted([r for r in recent if r.get('path') == '.github/workflows/' + WORKER
        and r['status'] == 'completed'], key=lambda r: r['created_at'], reverse=True)
    for run in completed:
        artifacts = pages(f"{base}/runs/{run['id']}/artifacts", 'artifacts')
        item = next((a for a in artifacts if a['name'] == 'futureeval-official-state' and not a['expired']), None)
        if item:
            control = next((a for a in artifacts if a['name'] == 'futureeval-official-control' and not a['expired']), item)
            with zipfile.ZipFile(io.BytesIO(gh(f"{base}/artifacts/{control['id']}/zip", binary=True))) as archive:
                if archive.getinfo('campaign.json').file_size > 10 * 1024 * 1024:
                    raise ValueError('Campaign exceeds listener control limit')
                state = json.loads(archive.read('campaign.json'))
            if state.get('schema') != 'official-competition-v1' or state.get('tournament') != 'fall-futureeval-2026':
                raise ValueError('Recovery identity mismatch')
            checkpoint_time = date(item['created_at'])
            break
        jobs = pages(f"{base}/runs/{run['id']}/jobs", 'jobs')
        if any(s['name'] == 'Acquire analyze and submit eligible questions' and s['conclusion'] != 'skipped'
               for job in jobs for s in job.get('steps', [])):
            raise ValueError('Latest executed worker checkpoint missing; refuse rediscovery')
    last = checkpoint_time or (max((date(r['created_at']) for r in recent), default=None))
    heartbeat = last is None or (now() - last).total_seconds() >= 86400
    reason = decision(ids, state, heartbeat=heartbeat)
    dispatched = reason != 'idle'
    if dispatched:
        if os.environ.get('MONITOR_DISPATCH_ENABLED', 'false').lower() != 'true':
            dispatched = False
            reason += '_dry_run'
        else:
            gh(f'{base}/workflows/{MONITOR}/dispatches', 'POST', {'ref': 'main'})
    return {'schema': 'public-monitor-health-v1', 'checked_at_utc': now().isoformat(),
        'open_question_count': len(ids), 'reason': reason, 'dispatched': dispatched,
        'open_scan_complete': True}


if __name__ == '__main__':
    try:
        report = run()
    except Exception as error:
        report = {'schema': 'public-monitor-health-v1', 'checked_at_utc': now().isoformat(),
            'status': 'failed', 'error_type': type(error).__name__, 'dispatched': False}
        if isinstance(error, HTTPError):
            report['metaculus_http_status'] = error.code
            report['response_format'] = 'json' if 'json' in error.headers.get('Content-Type', '') else 'non_json'
        print(json.dumps(report))
        with open('health.json', 'w', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
        raise SystemExit(1)
    with open('health.json', 'w', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
