# FutureEval public listener

This repository contains a small, read-only Metaculus listener. Forecasting,
research, budgets and submission receipts stay in the private repository.
Only `workflow_dispatch` runs this listener. There is no native Actions schedule.

## Required configuration

- Secret `METACULUS_TOKEN`: the authorized bot credential. Never print it.
- Secret `PRIVATE_ACTIONS_TOKEN`: a fine-grained PAT scoped only to
  `chenencc/futureeval-forecasting-agent`, with **Actions: write**.
- Variable `MONITOR_DISPATCH_ENABLED`: `true` after a successful dry-run test.
  Missing or `false` keeps the listener read-only without private dispatches.

The default workflow token is used only for this public repository's health
artifacts. Private evidence and responses must not be printed or uploaded here.
Do not enable privileged pull-request triggers or run untrusted contribution code
with these secrets. Restrict repository write access to trusted maintainers.

## cron-job.org

Change the existing job URL to:

```
https://api.github.com/repos/chenencc/futureeval-monitor/actions/workflows/monitor.yaml/dispatches
```

Use method `POST`, body `{"ref":"main"}`, and these headers:

```
Accept: application/vnd.github+json
Content-Type: application/json
Authorization: Bearer YOUR_PUBLIC_LISTENER_TOKEN
X-GitHub-Api-Version: 2026-03-10
```

Replace `YOUR_PUBLIC_LISTENER_TOKEN` with a separate fine-grained PAT restricted
to this public repository with **Actions: write**. Do not include angle brackets.
Run every five or ten minutes. Keep the old target until the new listener's
dry run and end-to-end dispatch have passed. A successful HTTP dispatch response
confirms acceptance, not completion; inspect the public Actions run and health.

For one-time cross-repository acceptance, manually run this workflow with
`dispatch_probe=true` after enabling dispatch. It requests an ordinary private
monitor/checkpoint refresh when idle. The input defaults to false; cron-job.org
does not need to pass it. Active-worker suppression remains in force.

## Behavior

1. Avoid dispatch while the private monitor or worker is active or just started.
2. Read the latest private official recovery ledger; fail closed if an executed
   worker's latest ledger is missing. Never reset a task or provider budget.
3. Page through open tournament questions, excluding practice questions and
   expired scoring windows.
4. Dispatch the private monitor for unseen eligible questions or due nonterminal
   tasks, including interrupted submission reconciliation after closing.
5. Refresh the private recovery checkpoint at least daily when otherwise idle.
6. Retain only three small public health artifacts. Private errors are redacted.

The private monitor still performs the complete authenticated eligibility scan.
The existing private worker rechecks own forecasts and official rules before
spending research quota or submitting. A dispatch is never recorded as a forecast.
