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

## Setup (one time)

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter key |
| `CLAUDE_CODE_OAUTH_TOKEN` | output of `claude setup-token` |
| `REMEDIATION_PUSH_TOKEN` *(optional)* | Fine-grained PAT for this repo with *Contents: read and write* and *Pull requests: read and write*. Pushes made with the default `GITHUB_TOKEN` don't trigger CI, so without it the workflow dispatches CI itself: the result shows on the fix commit and in Actions, but not in the PR's checks list. The script has already run the full suite before pushing either way. |

## Demo walkthrough

1. **Baseline is green.** The `CI` workflow passes on `poc-baseline` (556 tests).
2. **Renovate opens the PR.** Run Renovate (Node 24 required):
   ```sh
   RENOVATE_TOKEN=$(gh auth token) npx -y -p node@24 -p renovate@44 -- renovate <owner>/fastapi-users
   ```
   It opens `chore(deps): update dependency httpx to v0.28.1` from `renovate/httpx-0.x`.
3. **CI fails** with `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'app'`.
4. **Remediate.** Either:
   - GitHub: Actions → *Remediate dependency PR* → Run workflow → PR number, or
   - locally, with the PR branch checked out and its deps installed:
     ```sh
     gh pr checkout <N> && uv pip sync -p .venv requirements-test.txt
     .venv/Scripts/python tools/remediation/remediate.py --repo <owner>/fastapi-users --pr <N>
     ```
     Add `--dry-run` to analyse without committing, pushing or commenting.
5. **Result.** The PR gets a `remediation-bot` commit that changes `tests/conftest.py` to
   `transport=httpx.ASGITransport(app=app)` and a comment with the triage table. Compare with
   upstream's own fix in v14.0.0.

## Showing the other paths

- **`OTHER`:** run with `--log-file` pointing at a network or infra failure log. Jev classifies it
  `OTHER` and nothing is changed.
- **Tests fail after the fix / low confidence:** raise `--threshold 0.95`, or let a wrong edit
  fail. The script reverts and posts the analysis for human review.
