# Renovate Agentic Remediation (POC)

When a Renovate dependency update breaks the tests, an AI agent works out why, fixes the code, verifies the fix, and pushes it to the PR for a human to review.

## The scenario

- **Repository:** a fork of [`fastapi-users`](https://github.com/fastapi-users/fastapi-users), pinned to v13.0.0 on the `poc-baseline` branch.
- **Update:** Renovate bumps `httpx` from 0.27.2 to 0.28.1.
- **Breaking change:** httpx 0.28 removed the `app` argument of `httpx.AsyncClient`. The test fixture in `tests/conftest.py` uses it, so the test suite fails.

## How it works

| # | Step | Done by |
|---|---|---|
| 1 | Collect the CI failure log, the PR diff and the upstream release notes | Script |
| 2 | Classify the failure as `API_BREAK` or `OTHER`, with a confidence score | Jev (`typesafe/jev-1.13` via OpenRouter) |
| 3 | Explain the root cause and propose the code change | Claude (Sonnet, via Claude Code) |
| 4 | Decide whether the proposed change fixes the failure (`is_fixable`), with a confidence score | Jev |
| 5 | Check Renovate's Merge Confidence for the update | Script (from Renovate's PR label) |
| 6 | Apply the change and run the full test suite | Script |
| 7 | If the tests pass, commit and push the fix to the PR; otherwise revert | Script |
| 8 | Post a report on the PR | Script |

The fix is applied only when:

- the failure is classified as `API_BREAK` with confidence of at least 0.8;
- `is_fixable` has confidence of at least 0.8;
- Renovate's Merge Confidence for the update is `high` or `very high`;
- all tests pass after the change.

Otherwise, nothing is pushed and the report asks for human review. When Merge Confidence is below `high`, the report still includes Claude's root cause and proposed fix, but the fix isn't applied: low confidence means the update itself may be at fault, so a person should decide.

Merge Confidence is Renovate's rating of how safe an update is, based on how other projects fared with it (`low`, `neutral`, `high`, `very high`). Renovate adds it to the PR as a `merge-confidence:<level>` label. If the label is missing, Merge Confidence is treated as `unknown` and the fix isn't applied.

## Running the demo

Demo PRs are never merged. Merging would upgrade `poc-baseline` and leave Renovate nothing to propose.

### 1. Reset (skip on the first run)

1. Open the existing **Update dependency httpx to v0.28.1** pull request.
2. Click **Close pull request**.
3. Click **Delete branch**.

### 2. Let Renovate open the PR

1. Go to [developer.mend.io](https://developer.mend.io) and open `iriskamin-mend/fastapi-users`.
2. Run Renovate.
3. Within a few minutes, **Update dependency httpx to v0.28.1** appears under **Pull requests**, opened by `mend[bot]`.
4. The PR's **CI / test** check fails with `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'app'`.

### 3. Run the agent

1. Go to **Actions** → **Remediate dependency PR**.
2. Click **Run workflow**, enter the PR number, and click **Run workflow**.
3. Wait for the run to finish (about 2 minutes).

### 4. Approve CI on the fix

GitHub doesn't run CI automatically on commits pushed by a bot.

1. Open the PR.
2. In the checks section, click **Approve and run**.
3. Wait for **CI / test** to pass (about 1 minute).

### 5. Show the result

| PR tab | What to show |
|---|---|
| Conversation | The **Renovate agentic remediation fix** comment: the triage table (failure classification, Merge Confidence, is_fixable), root cause, upstream breaking change, the change made, the test result, and the agent timeline |
| Commits | `mend[bot]`'s dependency update, then `remediation-bot`'s fix |
| Files changed | `requirements-test.txt` (the update) and the agent's fix |
| Checks | **CI / test** passing |

## One-time setup

These repository secrets are already configured (**Settings** → **Secrets and variables** → **Actions**):

| Secret | Value |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key, used for Jev |
| `CLAUDE_CODE_OAUTH_TOKEN` | Token from `claude setup-token`, used for Claude |

## Running locally

With the PR branch checked out and its dependencies installed in `.venv` (Windows paths shown):

```sh
gh pr checkout <PR number>
uv pip sync -p .venv requirements-test.txt
.venv/Scripts/python tools/remediation/remediate.py --repo iriskamin-mend/fastapi-users --pr <PR number>
```

Options:

| Option | Effect |
|---|---|
| `--dry-run` | Analyse and test, but don't commit, push or comment |
| `--threshold 0.95` | Raise the confidence required for both Jev decisions |
| `--min-merge-confidence <level>` | Lowest Merge Confidence at which the fix is applied (default `high`) |
| `--merge-confidence <level>` | Use this Merge Confidence instead of the PR label, for example in local runs |
| `--log-file <path>` | Triage a saved failure log, for example a network error, to show an `OTHER` classification |

## Repository configuration

| File | Purpose |
|---|---|
| `requirements-test.txt` | Test dependencies pinned as of 2024-09-01, generated from `requirements-test.in` |
| `renovate.json` | Renovate proposes only the `httpx` update, labels the PR with its Merge Confidence, runs on this fork, recreates the PR after a reset, and doesn't rebase a branch once the fix is pushed |
| `.whitesource` | Turns off Mend's security scans, which aren't part of this demo |
| `.github/workflows/ci.yml` | Runs the test suite on every pull request |
| `.github/workflows/remediate.yml` | The **Remediate dependency PR** workflow |
| `tools/remediation/remediate.py` | The agent |
