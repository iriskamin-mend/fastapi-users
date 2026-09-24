# Autonomous dependency remediation (POC)

This fork of `fastapi-users` is pinned to **v13.0.0** with test dependencies frozen as of
2024-09-01 (`requirements-test.txt`, generated from `requirements-test.in`). Renovate proposes
one update, **httpx 0.27.2 → 0.28.1**. httpx 0.28 removed `AsyncClient(app=...)`, so every test
that uses the client fixture fails. `remediate.py` triages the failure and fixes it.

## Pipeline

| Step | Who | What |
|---|---|---|
| 1. Context | script | CI failure log (`gh run view --log-failed`, or a local pytest run), PR diff, bumped packages, upstream release notes (PyPI → GitHub Releases → CHANGELOG) |
| 2. Classify | **Jev** `typesafe/jev-1.13` | `choice`: `API_BREAK` / `OTHER` + confidence. Gate: `API_BREAK` and ≥ 0.8 |
| 3. Analyse | **Claude** (Claude Code CLI, `sonnet`) | Read-only repo access. Returns root cause, upstream change, fix summary, exact edits |
| 4. Fixability | **Jev** | `noul`: is_fixable probability for the proposed change. Gate: ≥ 0.8 |
| 5. Verify | script | Apply edits, run the full test suite. Green → commit + push to the PR branch. Red → revert |
| 6. Report | script | PR comment (+ job summary, `remediation-report.md`) with Jev's decisions and Claude's analysis |

Jev is called via OpenRouter's decisions endpoint (`POST /api/alpha/decisions`); it does not
support chat completions. Each Jev call costs about $0.00004; a Claude analysis about $0.04.

Renovate won't overwrite the fix: it stops updating a branch once someone else has committed
to it, and `rebaseWhen: conflicted` stops routine rebases.

Mend's SCA and SAST checks are turned off for this repo in `.whitesource`; they aren't part of
this demo.

## Setup (one time)

Repository secrets (Settings → Secrets and variables → Actions): `OPENROUTER_API_KEY` (OpenRouter
key) and `CLAUDE_CODE_OAUTH_TOKEN` (output of `claude setup-token`).

## Demo runbook

PRs are **not merged**: merging would move `poc-baseline` to httpx 0.28.1 and leave Renovate
nothing to propose. Reset instead (step 0).

0. **Reset** (skip on the very first run). If an httpx PR is open: open it → **Close pull
   request** → **Delete branch**. Check no `renovate/httpx-0.x` branch is left under
   *Branches*.
1. **Renovate opens the PR.** https://developer.mend.io → this repository → run Renovate. Within
   a few minutes *Update dependency httpx to v0.28.1* appears, authored by `mend[bot]` (the
   hosted Renovate app).
2. **CI fails.** On the PR, the `CI / test` check goes red:
   `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'app'`.
3. **Run the agent.** Actions → **Remediate dependency PR** → **Run workflow** → enter the PR
   number → **Run workflow**. Takes about 2 minutes.
4. **Approve CI on the fix.** GitHub holds CI for commits pushed by a bot. On the PR, scroll to
   the checks box (or open the **Checks** tab) and click **Approve and run**, then wait for
   `CI / test` to go green (about 1 minute).
5. **Show the result** on the PR:
   - the **Renovate agentic remediation fix** comment: triage table (failure classification,
     is_fixable, confidences), root cause, upstream breaking change, proposed diff, test result;
   - **Commits**: `mend[bot]`'s dependency bump, then `remediation-bot`'s one-line fix in
     `tests/conftest.py` (`transport=httpx.ASGITransport(app=app)`, the same fix upstream made in
     v14.0.0);
   - **Checks**: `CI / test` green.
6. **Leave the PR open**, or reset (step 0) for the next run.

Local alternative to step 3 (PR branch checked out, deps installed):

```sh
gh pr checkout <N> && uv pip sync -p .venv requirements-test.txt
.venv/Scripts/python tools/remediation/remediate.py --repo <owner>/fastapi-users --pr <N>
```

Add `--dry-run` to analyse without committing, pushing or commenting.

## Showing the other paths

- **`OTHER`:** run with `--log-file` pointing at a network or infra failure log. Jev classifies it
  `OTHER` and nothing is changed.
- **Tests fail after the fix / low confidence:** raise `--threshold 0.95`, or let a wrong edit
  fail. The script reverts and posts the analysis for human review.
