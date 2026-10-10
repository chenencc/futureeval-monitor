"""Read-only Metaculus gate; dispatch the private monitor only when work is due."""
import io
import json
import os
import subprocess
import time
import zipfile
from urllib.error import HTTPError
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from pathlib import Path
import importlib.util
_policy_path=Path(__file__).resolve().parent/'recovery_policy.py'
if not _policy_path.exists():_policy_path=Path(__file__).resolve().parents[1]/'recovery_policy.py'
_spec=importlib.util.spec_from_file_location('official_recovery_policy',_policy_path)
_policy=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_policy)
TERMINAL=_policy.TERMINAL

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


def open_ids(token, get=None, tournament=33121):
    if tournament not in {33121, 'minibench'}:
        raise ValueError('Unapproved listener tournament')
    url = 'https://www.metaculus.com/api/posts/?' + urlencode({
        'tournaments': tournament, 'statuses': 'open', 'limit': 100})
    ids = set()
    visited = set()
    def read(address):
        if get:return get(address)
        request = Request(address, headers={'Authorization': 'Token ' + token,
            'Accept': 'application/json', 'User-Agent': 'FutureEval-public-monitor/1.0'})
        for attempt in range(3):
            try:
                with build_opener(NoRedirect()).open(request, timeout=45) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:raise
                try:delay=min(45,max(1,int(error.headers.get('Retry-After','10'))))
                except ValueError:delay=10
                time.sleep(delay)
    while url:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != 'www.metaculus.com' or parsed.path != '/api/posts/':
            raise ValueError('Unapproved Metaculus pagination URL')
        if url in visited or len(visited) >= 100:
            raise ValueError('Metaculus pagination incomplete')
        visited.add(url)
        data = read(url)
        if not isinstance(data, dict):
            raise ValueError('Unexpected Metaculus response')
        rows = data.get('results', data.get('posts'))
        if not isinstance(rows, list):
            raise ValueError('Question list missing')
        for post in rows:
            if post.get('title', '').startswith('[PRACTICE]'):
                continue
            if post.get('group_of_questions') or post.get('conditional'):
                ident=str(post.get('id'))
                if not ident.isdecimal():raise ValueError('Invalid grouped post identity')
                detail=read(f'https://www.metaculus.com/api/posts/{ident}/?with_cp=false')
                if not isinstance(detail,dict) or detail.get('id')!=post['id']:
                    raise ValueError('Incomplete grouped post readback')
                post=detail
            qs = ([post['question']] if post.get('question') else []) + (post.get('group_of_questions') or {}).get('questions', [])
            qs += [v for k,v in (post.get('conditional') or {}).items()
                   if k in {'question_yes','question_no'} and isinstance(v,dict)]
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
        # Metaculus's infinite-count paginator can return next for an empty page.
        # Empty rows prove completion; following next would create a rate-limit loop.
        url = data.get('next') if rows else None
        if url and url.startswith('/'):
            url = 'https://www.metaculus.com' + url
    return ids


def decision(ids, state, *, active=False, heartbeat=False):
    if active:
        return 'worker_active'
    tasks = state.get('tasks', {})
    if any(t.get('stage')=='blocked_integrity' and t.get('last_error')==_policy.LEGACY_LOOKUP_ERROR and not t.get('lookup_recovery_applied') for t in tasks.values()):
        return 'recovery_due'
    if ids - tasks.keys():
        return 'new_questions'
    if any(t.get('stage') not in TERMINAL and
           (not t.get('retry_at_utc') or date(t['retry_at_utc']) <= now()) for t in tasks.values()):
        return 'pending_due'
    return 'checkpoint_refresh' if heartbeat else 'idle'


def run(*, tournament='fall-futureeval-2026', monitor=MONITOR, worker=WORKER,
        artifact_prefix='futureeval-official'):
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
    for workflow in (MONITOR, WORKER, 'minibench_monitor.yaml', 'minibench_competition.yaml'):
        recent.extend(gh(f'{base}/workflows/{workflow}/runs?per_page=30')['workflow_runs'])
    active = any(r['status'] != 'completed' or (now() - date(r['created_at'])).total_seconds() < 120 for r in recent)
    ids = open_ids(token, tournament='minibench' if tournament=='minibench' else 33121)
    if active:
        return {'schema': 'public-monitor-health-v1', 'checked_at_utc': now().isoformat(),
            'reason': 'worker_active', 'dispatched': False,
            'open_question_count': len(ids), 'open_scan_complete': True}
    state = {'tasks': {}}
    checkpoint_time = None
    completed = sorted([r for r in recent if r.get('path') == '.github/workflows/' + worker
        and r['status'] == 'completed'], key=lambda r: r['created_at'], reverse=True)
    for run in completed:
        artifacts = pages(f"{base}/runs/{run['id']}/artifacts", 'artifacts')
        item = next((a for a in artifacts if a['name'] == artifact_prefix+'-state' and not a['expired']), None)
        if item:
            control = next((a for a in artifacts if a['name'] == artifact_prefix+'-control' and not a['expired']), item)
            with zipfile.ZipFile(io.BytesIO(gh(f"{base}/artifacts/{control['id']}/zip", binary=True))) as archive:
                if archive.getinfo('campaign.json').file_size > 10 * 1024 * 1024:
                    raise ValueError('Campaign exceeds listener control limit')
                state = json.loads(archive.read('campaign.json'))
            if state.get('schema') != 'official-competition-v1' or state.get('tournament') != tournament:
                raise ValueError('Recovery identity mismatch')
            checkpoint_time = date(item['created_at'])
            break
        jobs = pages(f"{base}/runs/{run['id']}/jobs", 'jobs')
        if any(s['name'] == 'Acquire analyze and submit eligible questions' and s['conclusion'] != 'skipped'
               for job in jobs for s in job.get('steps', [])):
            raise ValueError('Latest executed worker checkpoint missing; refuse rediscovery')
    own_runs=[r for r in recent if r.get('path') in {'.github/workflows/'+monitor, '.github/workflows/'+worker}]
    last = checkpoint_time or (max((date(r['created_at']) for r in own_runs), default=None))
    heartbeat = last is None or (now() - last).total_seconds() >= 86400
    task_health = _policy.health(state.get('tasks',{}))
    reason = decision(ids, state, heartbeat=heartbeat)
    if reason == 'idle' and os.environ.get('MONITOR_DISPATCH_PROBE', 'false').lower() == 'true':
        reason = 'acceptance_checkpoint_refresh'
    dispatched = reason != 'idle'
    if dispatched:
        if os.environ.get('MONITOR_DISPATCH_ENABLED', 'false').lower() != 'true':
            dispatched = False
            reason += '_dry_run'
        else:
            gh(f'{base}/workflows/{monitor}/dispatches', 'POST', {'ref': 'main'})
    return {'schema': 'public-monitor-health-v1', 'checked_at_utc': now().isoformat(),
        'open_question_count': len(ids), 'reason': reason, **task_health, 'dispatched': dispatched,
        'open_scan_complete': True}


def run_all():
    profiles=[{'tournament':'fall-futureeval-2026','monitor':MONITOR,'worker':WORKER,
               'artifact_prefix':'futureeval-official'}]
    if os.environ.get('MINIBENCH_LISTEN_ENABLED','false').lower()=='true':
        profiles.append({'tournament':'minibench','monitor':'minibench_monitor.yaml',
                         'worker':'minibench_competition.yaml','artifact_prefix':'minibench-official'})
        # Alternate priority each ten minutes so neither tournament monopolizes
        # a continuing queue. At most one monitor is dispatched per cron tick.
        if int(now().timestamp())//600%2:profiles.reverse()
    reports={};dispatched=False
    for profile in profiles:
        if dispatched:
            reports[profile['tournament']]={'reason':'dispatch_serialized','dispatched':False}
            continue
        try:
            report=run(**profile)
        except Exception as error:
            report={'reason':'failed','status':'failed','error_type':type(error).__name__,'dispatched':False}
            if isinstance(error,HTTPError):report['metaculus_http_status']=error.code
        reports[profile['tournament']]=report
        dispatched=report.get('dispatched',False)
    if len(reports)==1:
        report=next(iter(reports.values()))
    else:
        report={'schema':'public-monitor-health-v1','checked_at_utc':now().isoformat(),
                'dispatched':dispatched,'reason':'multi_tournament',
                'open_question_count':sum(r.get('open_question_count',0) for r in reports.values()),
                'tournaments':reports}
    if any(r.get('status')=='failed' for r in reports.values()):report['status']='failed'
    return report


if __name__ == '__main__':
    try:
        report = run_all()
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
    if report.get('status')=='failed':raise SystemExit(1)
