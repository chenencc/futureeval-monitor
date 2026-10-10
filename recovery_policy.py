"""Shared worker, listener and storage lifecycle policy; no network actions."""
from datetime import datetime, timezone, timedelta

TERMINAL = {'accepted', 'already_forecasted', 'closed', 'deadline_missed',
            'blocked_integrity', 'provider_blocked', 'platform_rejected', 'needs_review'}
ATTENTION = {'blocked_integrity', 'provider_blocked', 'platform_rejected',
             'submission_unknown', 'needs_review'}
LEGACY_LOOKUP_ERROR = 'ValueError: Read only URLs already saved in this task'


def recover_legacy_lookup(task, *, has_input, has_submission):
    """Reopen only the diagnosed local lookup defect, never arbitrary blocks."""
    if (task.get('stage') == 'blocked_integrity' and task.get('last_error') == LEGACY_LOOKUP_ERROR
            and has_input and not has_submission and not task.get('lookup_recovery_applied')):
        task.update(stage='recovery_wait', retry_at_utc=None, lookup_recovery_applied=True,
                    recovery_action='resume_saved_analysis', previous_error=task['last_error'])
        return True
    return False


def classify(exc, task, now):
    from ForecastAgent.readers.saved import SavedSourceLookupError, SavedSourceIdentityError
    message = str(exc)
    if isinstance(exc, SavedSourceIdentityError) or (isinstance(exc, ValueError) and
            any(s in message.lower() for s in ('identity', 'hash', 'changed', 'mismatch', 'frozen', 'unapproved'))):
        return 'blocked_integrity', None
    if 'cap exhausted' in message.lower() or 'quota' in message.lower():
        return 'provider_blocked', None
    if 'closed' in message.lower() or 'deadline' in message.lower():
        return 'deadline_missed', None
    failures = task.get('recoverable_failures', 0) + 1
    task['recoverable_failures'] = failures
    if failures >= 3:
        return 'needs_review', None
    delay = 60 if isinstance(exc, SavedSourceLookupError) else 120
    retry = now + timedelta(seconds=delay)
    deadline = task.get('deadline_utc')
    if deadline:
        end = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        if (end - now).total_seconds() <= 1800:
            retry = now
    return ('recovery_wait' if isinstance(exc, ValueError) else 'retry_wait'), retry.isoformat()


def health(tasks, now=None):
    now = now or datetime.now(timezone.utc)
    at_risk = []
    attention = []
    for task in tasks.values():
        if task.get('stage') in ATTENTION:
            attention.append(task['id'])
        if task.get('stage') in {'accepted', 'already_forecasted', 'closed', 'deadline_missed'}:
            continue
        if task.get('deadline_utc'):
            seconds = (datetime.fromisoformat(task['deadline_utc'].replace('Z', '+00:00')) - now).total_seconds()
            if seconds <= 1800:
                at_risk.append(task['id'])
    return {'attention_ids': sorted(attention), 'deadline_at_risk_ids': sorted(at_risk),
            'healthy': not attention and not at_risk}
