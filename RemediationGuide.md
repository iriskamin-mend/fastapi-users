# Renovate Agentic Remediation: Guide

This guide explains how to set up, run, reset, and troubleshoot the Renovate agentic remediation flow, and what it takes to use it on another repository.

## 1. What the flow does

When a Renovate dependency update breaks the tests, an AI agent works out why, fixes the code, verifies the fix, and pushes it to the PR for a human to review and merge.

| # | Step | Done by |
|---|---|---|
| 1 | Collect the CI failure log, the PR diff and the upstream release notes | Script |
| 2 | Classify the failure as `API_BREAK` or `OTHER`, with a confidence score | Jev (`typesafe/jev-1.13` via OpenRouter) |
| 3 | Explain the root cause and propose the code change | Claude (Sonnet, via Claude Code) |
| 4 | Decide whether the proposed change fixes the failure (`is_fixable`), with a confidence score | Jev |
| 5 | Apply the change and run the full test suite | Script |
| 6 | If the tests pass, commit and push the fix to the PR; otherwise revert | Script |
| 7 | Post a report on the PR | Script |

The fix is pushed only when all three conditions hold:

- the failure is classified as `API_BREAK` with confidence of at least 0.8;
- `is_fixable` has confidence of at least 0.8;
- all tests pass after the change.

Otherwise nothing is pushed, and the report on the PR asks for human review.

### The demo scenario

- **Repository:** [`iriskamin-mend/fastapi-users`](https://github.com/iriskamin-mend/fastapi-users), a fork of `fastapi-users` pinned to v13.0.0 on the `poc-baseline` branch.
- **Update:** Renovate bumps `httpx` from 0.27.2 to 0.28.1.
- **Breaking change:** httpx 0.28 removed the `app` argument of `httpx.AsyncClient`. The test fixture in `tests/conftest.py` uses it, so the test suite fails.
- **Expected fix:** replace `app=app` with `transport=httpx.ASGITransport(app=app)`. The upstream maintainers made the same change in v14.0.0.

## 2. One-time setup

All of this is already done for the demo repository. It's listed here for reference and for setting up a new repository.

### Accounts and access

| Need | Used for |
|---|---|
| GitHub repository with Actions enabled | CI and the remediation workflow |
| Mend Renovate app installed on the repository ([developer.mend.io](https://developer.mend.io)) | Opening dependency update PRs as `mend[bot]` |
| OpenRouter account with credit | Jev decisions (about $0.00004 per call) |
| Claude subscription or Anthropic API key | Claude analysis (about $0.03–$0.05 per run) |

### Repository secrets

Add these under **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

| Secret | How to get it |
|---|---|
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) → **Create Key** |
| `CLAUDE_CODE_OAUTH_TOKEN` | Run `claude setup-token` in a terminal, sign in, and copy the token it prints |

Paste secrets through the GitHub page or `gh secret set <NAME>`. Don't pipe them from Windows PowerShell 5.1: it adds an invisible character to the start of the value.

### Repository files

| File | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Runs the test suite on every pull request |
| `.github/workflows/remediate.yml` | The **Remediate dependency PR** workflow |
| `tools/remediation/remediate.py` | The agent |
| `renovate.json` | Renovate configuration (see below) |
| `.whitesource` | Turns off Mend's security scans, which aren't part of this demo |
| `requirements-test.txt` | Test dependencies pinned as of 2024-09-01, generated from `requirements-test.in` |

The `renovate.json` settings that matter for this flow:

| Setting | Why |
|---|---|
| `"forkProcessing": "enabled"` | The Mend Renovate app skips forked repositories without it |
| `"recreateWhen": "always"` | Renovate reopens the update after a demo PR is closed |
| `"rebaseWhen": "conflicted"` | Renovate doesn't rebase the branch routinely. It also stops updating a branch once someone else has committed to it, so the fix isn't overwritten. |
| `packageRules` with `"!httpx"` disabled | Demo only: Renovate proposes just the httpx update |
| `baseBranchPatterns: ["poc-baseline"]` | Demo only: Renovate targets the pinned baseline branch |

## 3. Running the demo

Demo PRs are never merged. Merging would upgrade `poc-baseline` and leave Renovate nothing to propose. Reset instead (step 3.1).

### 3.1 Reset (skip on the first run)

1. Open the existing **Update dependency httpx to v0.28.1** pull request.
2. Click **Close pull request**.
3. Click **Delete branch**.
4. Optionally, check under **Branches** that `renovate/httpx-0.x` is gone.

### 3.2 Let Renovate open the PR

1. Go to [developer.mend.io](https://developer.mend.io) and open `iriskamin-mend/fastapi-users`.
2. Run Renovate.
3. Within a few minutes, **Update dependency httpx to v0.28.1** appears under **Pull requests**, opened by `mend[bot]`.
4. The PR's **CI / test** check fails with `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'app'`.

### 3.3 Run the agent

1. Go to **Actions** → **Remediate dependency PR**.
2. Click **Run workflow**, enter the PR number, and click **Run workflow**.
3. Wait for the run to finish (about 2 minutes).

Tick **dry_run** to analyse and test without committing, pushing or commenting. The report then appears only on the workflow run's summary page.

### 3.4 Approve CI on the fix

GitHub doesn't run CI automatically on commits pushed by a bot.

1. Open the PR.
2. In the checks section, click **Approve and run**.
3. Wait for **CI / test** to pass (about 1 minute).

### 3.5 Show the result

| Where | What to show |
|---|---|
| PR → Conversation | The **Renovate agentic remediation fix** comment: the triage table (failure classification, is_fixable, confidence scores), root cause, upstream breaking change, the change made, and the test result |
| PR → Commits | `mend[bot]`'s dependency update, then `remediation-bot`'s fix |
| PR → Files changed | `requirements-test.txt` (the update) and `tests/conftest.py` (the one-line fix) |
| PR → Checks | **CI / test** passing |
| Actions → the remediation run | The same report on the summary page, `remediation-report.md` under **Artifacts**, and Jev's raw answers in the **Triage and remediate** step log |

## 4. Running locally

Use this to develop the agent or to demo without GitHub Actions. You need Python 3.12, [uv](https://docs.astral.sh/uv/), the GitHub CLI (`gh`, logged in), and Claude Code (`claude`, logged in). `OPENROUTER_API_KEY` must be set as an environment variable.

```sh
git clone https://github.com/iriskamin-mend/fastapi-users.git
cd fastapi-users
uv venv -p 3.12 .venv
gh pr checkout <PR number>
uv pip sync -p .venv requirements-test.txt
.venv/Scripts/python tools/remediation/remediate.py --repo iriskamin-mend/fastapi-users --pr <PR number>
```

On macOS or Linux, use `.venv/bin/python` instead of `.venv/Scripts/python`.

| Option | Effect |
|---|---|
| `--dry-run` | Analyse and test, but don't commit, push or comment |
| `--threshold 0.95` | Raise the confidence required for both decisions |
| `--log-file <path>` | Triage a saved failure log, for example a network error, to show an `OTHER` classification |
| `--local-log` | Reproduce the failure by running the tests locally instead of reading the CI log |
| `--test-cmd "<command>"` | Use a different test command (default: `python -m pytest`) |

The report is also saved to `remediation-report.md`.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Renovate runs but opens no PR | The Renovate app skips forks | Make sure `renovate.json` on the default branch has `"forkProcessing": "enabled"` |
| Renovate opens no PR after a reset | The closed PR's branch still exists, or `recreateWhen` is missing | Delete the `renovate/httpx-0.x` branch; check `"recreateWhen": "always"` |
| The PR's CI doesn't run on the fix | GitHub holds CI for commits pushed by a bot | Click **Approve and run** on the PR |
| Workflow fails with `UnicodeEncodeError ... '﻿'` | The OpenRouter secret starts with an invisible character | Re-create the secret by pasting it on the GitHub page |
| Workflow fails at the Claude step | `CLAUDE_CODE_OAUTH_TOKEN` is missing or expired | Run `claude setup-token` again and update the secret |
| Report says "Failure not caused by a dependency API change" | Jev classified the failure as `OTHER` (for example, infrastructure) | Expected for non-API failures; check the CI log |
| Report says "needs human review" | A confidence score was below 0.8, the change couldn't be applied, or the tests still failed | Read the report's triage table and verification section |
| Mend security checks appear on PRs | Mend security services are on for the repository | Turn them off in the Mend developer portal; `.whitesource` already disables the scans |

## 6. Using the flow on another repository

The core of the flow is generic: the triage and fix decisions, the confidence thresholds, the test-before-push rule, and the PR report don't depend on this repository. Some inputs and set-up steps are currently specific to Python projects that pin dependencies in a `requirements*.txt` file.

### What works on any repository today

- Reading the failed CI log from GitHub Actions, and the PR diff.
- Jev's two decisions, and Claude's root cause and fix. Claude explores the repository itself, so it isn't tied to this codebase.
- Applying the change, running the tests, and committing, pushing and commenting only if they pass.
- The **Remediate dependency PR** workflow trigger and the PR report.

### What is specific today, and what to change

| Area | Current behaviour | To support other repositories |
|---|---|---|
| Detecting the updated package | Parses `package==version` lines in the PR diff (pip requirements files) | Read the package table in the Renovate PR description, which has the same format for every ecosystem (npm, Maven, Go, and others) |
| Release notes | Looked up through PyPI, then GitHub Releases | Use the **Release Notes** section Renovate already includes in the PR description |
| Test command | `python -m pytest` | Set `--test-cmd` (for example `npm test`, `mvn -q test`, `go test ./...`) |
| Failure log extraction | Tuned to pytest output; other output falls back to the end of the log | Add patterns for other test runners, or rely on the fallback |
| Workflow set-up steps | Installs Python 3.12 and `requirements-test.txt` | Replace with the repository's own build set-up (Node, Java, Go, and so on) |
| Claude's instructions | Written for a Python engineer | Make the language a parameter |
| Renovate configuration | Limited to httpx on `poc-baseline` | Remove the demo-only `packageRules` and `baseBranchPatterns` |

### Steps to add the flow to another Python repository today

1. Copy `tools/remediation/remediate.py` and `.github/workflows/remediate.yml` into the repository.
2. In `remediate.yml`, change the **Install PR's pinned test dependencies** step to the repository's install command.
3. If the tests aren't run with plain `pytest`, add `--test-cmd "<command>"` to the **Triage and remediate** step.
4. Add the `OPENROUTER_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` secrets.
5. Make sure CI runs on pull requests, and that Renovate is enabled with `"rebaseWhen": "conflicted"`.
6. Run the workflow on a failing Renovate PR.
