"""Autonomous triage and remediation of breaking dependency updates.

Flow (see tools/remediation/README.md):
  1. Collect context: CI failure log, PR diff, dependency bumps, upstream release notes.
  2. Jev (typesafe/jev-1.13 on OpenRouter) classifies the failure: API_BREAK or OTHER.
  3. Claude (Claude Code CLI, headless) writes the root cause and proposes exact edits.
  4. Jev decides is_fixable for the proposed change.
  5. Gate passed -> apply edits, run the full test suite, and only if green commit + push.
     Otherwise revert and post the analysis for human review.

Stdlib only, so it runs in any CI image that has Python, git, gh and the claude CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

JEV_MODEL = "typesafe/jev-1.13"
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
CLAUDE_MODEL = "sonnet"  # explicit model; never Fable (org data policy)
MAX_LOG_CHARS = 12_000
MAX_NOTES_CHARS = 8_000
MAX_DIFF_CHARS = 6_000
COMMENT_MARKER = "<!-- agentic-remediation -->"


# --------------------------------------------------------------------------- utils


def log(msg: str) -> None:
    print(f"[remediate] {msg}", file=sys.stderr, flush=True)


def run(
    cmd: list[str] | str, *, check: bool = True, input: Optional[str] = None
) -> subprocess.CompletedProcess[str]:
    shell = isinstance(cmd, str)
    proc = subprocess.run(
        cmd,
        shell=shell,
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {cmd}\n{proc.stdout}\n{proc.stderr}"
        )
    return proc


def http_json(url: str, *, data: Any = None, headers: Optional[dict] = None) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def clip(text: str, limit: int, *, keep: str = "head") -> str:
    if len(text) <= limit:
        return text
    if keep == "tail":
        return "…[truncated]…\n" + text[-limit:]
    return text[:limit] + "\n…[truncated]…"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Trace:
    """Step-by-step record of the run: inputs, outputs, reasoning, tokens and cost per step.

    Written to remediation-trace.json (uploaded as a workflow artifact) and summarized in the
    PR report's "Agent timeline".
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"started_at": now_iso(), "steps": []}

    @contextmanager
    def step(self, name: str, actor: str) -> Iterator[dict]:
        entry: dict[str, Any] = {"step": name, "actor": actor, "started_at": now_iso()}
        self.data["steps"].append(entry)
        t0 = time.monotonic()
        try:
            yield entry
        except BaseException as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            entry["duration_s"] = round(time.monotonic() - t0, 2)

    def totals(self) -> dict:
        tot = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        for s in self.data["steps"]:
            u = s.get("usage") or {}
            tot["input_tokens"] += u.get("input_tokens", 0)
            tot["output_tokens"] += u.get("output_tokens", 0)
            tot["cost_usd"] += u.get("cost_usd", 0.0)
        tot["cost_usd"] = round(tot["cost_usd"], 6)
        return tot

    def write(self, path: str, **run_info: Any) -> None:
        self.data.update(run_info, finished_at=now_iso(), totals=self.totals())
        Path(path).write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


TRACE = Trace()


def openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key and sys.platform == "win32":
        # A key saved as a Windows user variable is not visible to already-running shells.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")[0]
        except OSError:
            key = None
    # Secrets piped in from Windows PowerShell can carry a BOM and trailing newline.
    key = (key or "").strip().lstrip("﻿")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set")
    return key


# --------------------------------------------------------------------------- context


@dataclass
class Bump:
    package: str
    old: str
    new: str


@dataclass
class Context:
    repo: Optional[str]
    pr: Optional[int]
    branch: str
    diff: str
    failure_log: str
    log_source: str
    bumps: list[Bump] = field(default_factory=list)
    release_notes: str = ""


REQ_LINE = re.compile(r"^([+-])\s*([A-Za-z0-9_.\-\[\]]+)==([^\s;#]+)", re.M)


def parse_bumps(diff: str) -> list[Bump]:
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for sign, name, version in REQ_LINE.findall(diff):
        key = re.sub(r"\[.*\]", "", name).lower().replace("_", "-")
        (removed if sign == "-" else added)[key] = version
    return [Bump(p, removed[p], added[p]) for p in added if p in removed and removed[p] != added[p]]


def version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.findall(r"\d+|[a-z]+", v.lower()))


def fetch_release_notes(bump: Bump) -> str:
    """Upstream release notes between old (exclusive) and new (inclusive).

    Order of sources: PyPI metadata -> GitHub Releases API -> CHANGELOG.md at the new tag.
    """
    try:
        info = http_json(f"https://pypi.org/pypi/{bump.package}/json")["info"]
    except Exception as e:  # noqa: BLE001
        return f"(could not read PyPI metadata for {bump.package}: {e})"

    urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
    gh_repo = next(
        (
            m.group(1).removesuffix(".git")
            for u in urls
            if (m := re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", u or ""))
        ),
        None,
    )
    if not gh_repo:
        return f"(no GitHub repository found in PyPI metadata for {bump.package})"

    lo, hi = version_tuple(bump.old), version_tuple(bump.new)
    try:
        out = run(["gh", "api", f"repos/{gh_repo}/releases?per_page=50"]).stdout
        releases = json.loads(out)
    except Exception:  # noqa: BLE001
        releases = []
    picked = []
    for rel in releases:
        v = version_tuple(rel.get("tag_name", "").lstrip("v"))
        if lo < v <= hi and rel.get("body"):
            picked.append(f"## {bump.package} {rel['tag_name']}\n{rel['body'].strip()}")
    if picked:
        return f"Source: https://github.com/{gh_repo}/releases\n\n" + "\n\n".join(reversed(picked))

    for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md"):
        for tag in (bump.new, f"v{bump.new}"):
            p = run(
                ["gh", "api", f"repos/{gh_repo}/contents/{name}?ref={tag}", "--jq", ".content"],
                check=False,
            )
            if p.returncode == 0 and p.stdout.strip():
                import base64

                text = base64.b64decode(p.stdout).decode("utf-8", "replace")
                start = text.find(bump.new)
                end = text.find(bump.old, start + 1) if start >= 0 else -1
                section = text[start:end] if start >= 0 and end > start else text[:4000]
                return f"Source: {gh_repo}/{name}@{tag}\n\n{section}"
    return f"(no release notes found for {bump.package} {bump.old}->{bump.new} in {gh_repo})"


def error_digest(output: str) -> str:
    """Collapse repeated exceptions into counts (summary lines are truncated; "E" lines are not)."""
    counts: dict[str, int] = {}
    for msg in re.findall(r"^E\s+(\w+(?:Error|Exception|Exit)\b.*)$", output, re.M):
        counts[msg] = counts.get(msg, 0) + 1
    lines = [f"{n}x  {msg}" for msg, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5]]
    result = re.findall(r"^=*\s*(\d+ (?:failed|passed|error).*?) in [\d.]+s", output, re.M)
    return "\n".join(([f"result: {result[-1]}"] if result else []) + lines)


def extract_pytest_failure(output: str) -> str:
    """First failure/error block plus a de-duplicated error digest: enough for triage."""
    m = re.search(r"^=+ (ERRORS|FAILURES) =+$", output, re.M)
    body = output[m.start():] if m else output
    first = re.split(r"^_{3,} .+ _{3,}$", body, flags=re.M)
    head = "".join(first[:2]) if len(first) > 1 else body
    digest = error_digest(output) or clip(output, 3000, keep="tail")
    return clip(head.strip(), MAX_LOG_CHARS - 3500) + "\n\n--- error digest ---\n" + digest


def ci_failure_log(repo: str, branch: str) -> Optional[tuple[str, str]]:
    p = run(
        ["gh", "run", "list", "-R", repo, "--branch", branch, "--status", "failure",
         "--limit", "1", "--json", "databaseId"],
        check=False,
    )
    runs = json.loads(p.stdout or "[]") if p.returncode == 0 else []
    if not runs:
        return None
    run_id = runs[0]["databaseId"]
    out = run(["gh", "run", "view", str(run_id), "-R", repo, "--log-failed"], check=False).stdout
    # gh prefixes each line with "<job>\t<step>\t<timestamp> "; strip for readability.
    lines = [re.sub(r"^[^\t]*\t[^\t]*\t\S+ ", "", ln) for ln in out.splitlines()]
    return f"GitHub Actions run {run_id}", extract_pytest_failure("\n".join(lines))


def collect_context(args: argparse.Namespace) -> Context:
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if args.pr:
        info = json.loads(
            run(["gh", "pr", "view", str(args.pr), "-R", args.repo, "--json", "headRefName,baseRefName"]).stdout
        )
        branch = info["headRefName"]
        diff = run(["gh", "pr", "diff", str(args.pr), "-R", args.repo]).stdout
    else:
        diff = run(["git", "diff", f"{args.base}...HEAD"]).stdout

    failure_log, source = None, ""
    if args.log_file:
        failure_log, source = Path(args.log_file).read_text(encoding="utf-8"), args.log_file
    elif args.pr and not args.local_log:
        found = ci_failure_log(args.repo, branch)
        if found:
            source, failure_log = found
    if failure_log is None:
        log("reproducing the failure locally")
        p = run(args.test_cmd, check=False)
        if p.returncode == 0:
            sys.exit("tests pass on this branch; nothing to remediate")
        failure_log, source = extract_pytest_failure(p.stdout + p.stderr), f"local run: {args.test_cmd}"

    ctx = Context(args.repo, args.pr, branch, diff, clip(failure_log, MAX_LOG_CHARS), source)
    ctx.bumps = parse_bumps(diff)
    ctx.release_notes = clip(
        "\n\n".join(fetch_release_notes(b) for b in ctx.bumps) or "(no dependency bumps detected in diff)",
        MAX_NOTES_CHARS,
    )
    return ctx


# --------------------------------------------------------------------------- Jev


def jev(step: dict, state: str | dict, questions: dict) -> dict:
    request = {"model": JEV_MODEL, "state": state, "questions": questions}
    step["request"] = request  # Jev returns decisions only, so its input is the closest thing to a rationale
    resp = http_json(JEV_URL, data=request, headers={"Authorization": f"Bearer {openrouter_key()}"})
    usage = resp.get("usage", {})
    step.update(
        model=resp.get("model", JEV_MODEL),
        generation_id=resp.get("id"),
        answers=resp["answers"],
        usage={
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cost_usd": usage.get("cost", 0.0),
        },
    )
    log(f"jev answers={json.dumps(resp['answers'])} cost=${usage.get('cost')}")
    return resp["answers"]


def bumps_text(ctx: Context) -> str:
    return ", ".join(f"{b.package} {b.old} -> {b.new}" for b in ctx.bumps) or "unknown"


def jev_classify(ctx: Context) -> tuple[str, float]:
    state = {
        "event": "CI tests failed on a dependency update PR; the PR changes nothing but the dependency pin.",
        "dependency_change": bumps_text(ctx),
        "failure_log": ctx.failure_log,
        "upstream_release_notes": clip(ctx.release_notes, 4000),
    }
    questions = {
        "category": {
            "type": "choice",
            "instructions": "Classify the cause of this CI test failure that occurred after a dependency update.",
            "criteria": {
                "API_BREAK": "The updated dependency removed, renamed, or changed an API, signature, "
                             "or behavioral contract that this code relies on.",
                "OTHER": "Flaky test, network, environment, or infrastructure failure unrelated to "
                         "the updated dependency's API.",
            },
        }
    }
    with TRACE.step("Failure classification", "Jev") as step:
        a = jev(step, state, questions)["category"]
        choice, conf = a["choice"], float(a.get("confidence", a["probabilities"][a["choice"]]))
        step["decision"] = f"{choice} (confidence {conf:.2f})"
    return choice, conf


def jev_fixable(ctx: Context, analysis: dict) -> float:
    edits = "\n".join(
        f"--- {e['file']}\n- {e['old_string']}\n+ {e['new_string']}" for e in analysis["edits"]
    )
    state = {
        "dependency_change": bumps_text(ctx),
        "failure_log": clip(ctx.failure_log, 4000),
        "root_cause": analysis["root_cause"],
        "breaking_upstream_change": analysis["breaking_change"],
        "proposed_fix": analysis["fix_summary"],
        "proposed_edits": edits or "(none)",
    }
    questions = {
        "is_fixable": {
            "type": "noul",
            "instructions": "Is this failure fixable by the proposed targeted code change in this "
                            "repository, keeping the new dependency version and without side effects?",
        }
    }
    with TRACE.step("Fixability (is_fixable)", "Jev") as step:
        score = float(jev(step, state, questions)["is_fixable"]["noul"])
        step["decision"] = f"is_fixable={str(score >= 0.5).lower()} (confidence {score:.2f})"
    return score


# --------------------------------------------------------------------------- Claude

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string", "description": "Why the tests fail, citing file:line."},
        "breaking_change": {"type": "string", "description": "The upstream API change, quoting release notes."},
        "fix_summary": {"type": "string", "description": "The code adjustment required, in 1-3 sentences."},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Repo-relative path."},
                    "old_string": {"type": "string", "description": "Exact text to replace; must occur once."},
                    "new_string": {"type": "string"},
                },
                "required": ["file", "old_string", "new_string"],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["root_cause", "breaking_change", "fix_summary", "edits", "confidence"],
}

SYSTEM_PROMPT = (
    "You are a senior Python engineer remediating a breaking dependency update in the current "
    "repository. Investigate with the read-only tools, then answer in the required JSON schema. "
    "Adapt the code to the NEW dependency API; never pin, downgrade, or edit requirement files. "
    "Keep edits minimal. Every old_string must match the file exactly (including indentation) and "
    "occur exactly once; include surrounding lines if needed to make it unique. "
    "Ignore tools/remediation/ (this remediation tooling) and don't edit it."
)


def claude_analyze(ctx: Context) -> dict:
    prompt = textwrap.dedent(f"""\
        A dependency update broke the test suite on branch `{ctx.branch}`.

        Dependency changes: {bumps_text(ctx)}

        ## Failure log ({ctx.log_source})
        ```
        {ctx.failure_log}
        ```

        ## PR diff
        ```diff
        {clip(ctx.diff, MAX_DIFF_CHARS)}
        ```

        ## Upstream release notes
        {ctx.release_notes}

        Find every usage in the repository affected by the breaking change (not only the one in
        the log) and propose the edits that fix it.
        """)
    cmd = [
        "claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "stream-json", "--verbose",
        "--tools", "Read,Grep,Glob", "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(ANALYSIS_SCHEMA), "--strict-mcp-config",
    ]
    with TRACE.step("Root cause and fix", "Claude") as step:
        step["request"] = {"model": CLAUDE_MODEL, "system_prompt": SYSTEM_PROMPT, "prompt": prompt}
        log(f"asking Claude ({CLAUDE_MODEL}) for root cause and fix")
        p = run(cmd, input=prompt, check=False)
        transcript, result = parse_claude_stream(p.stdout)
        step["transcript"] = transcript
        if result is None:
            raise RuntimeError(f"claude CLI returned no result:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
        usage = result.get("usage") or {}
        step.update(
            model=", ".join((result.get("modelUsage") or {}).keys()) or CLAUDE_MODEL,
            turns=result.get("num_turns"),
            usage={
                "input_tokens": usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cost_usd": result.get("total_cost_usd", 0.0),
            },
        )
        if result.get("is_error") or not result.get("structured_output"):
            raise RuntimeError(f"claude CLI error: {result.get('result')}")
        analysis = result["structured_output"]
        step["answer"] = analysis
        step["decision"] = f"{len(analysis['edits'])} edit(s), self-reported confidence {analysis['confidence']:.2f}"
        log(f"claude cost=${result.get('total_cost_usd')} turns={result.get('num_turns')}")
    return analysis


def parse_claude_stream(stdout: str) -> tuple[list[dict], Optional[dict]]:
    """Turn Claude Code's stream-json events into a readable transcript plus the final result."""
    transcript: list[dict] = []
    result = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "result":
            result = event
        elif kind in ("assistant", "user"):
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    transcript.append({"type": "note", "text": block["text"]})
                elif btype == "thinking" and block.get("thinking", "").strip():
                    transcript.append({"type": "thinking", "text": block["thinking"]})
                elif btype == "tool_use" and block.get("name") != "StructuredOutput":
                    transcript.append({"type": "tool_call", "tool": block.get("name"), "input": block.get("input")})
                elif btype == "tool_result":
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                    if body and "Structured output provided" not in str(body):
                        transcript.append({"type": "tool_result", "content": clip(str(body), 3000)})
    return transcript, result


# --------------------------------------------------------------------------- apply / verify


def apply_edits(edits: list[dict]) -> list[str]:
    touched = []
    for e in edits:
        path = Path(e["file"])
        if path.name.startswith("requirements") or path.suffix in {".lock", ".toml"}:
            raise ValueError(f"refusing to edit dependency file {path}")
        text = path.read_text(encoding="utf-8")
        if text.count(e["old_string"]) != 1:
            raise ValueError(f"old_string occurs {text.count(e['old_string'])}x in {path}, expected 1")
        path.write_text(text.replace(e["old_string"], e["new_string"]), encoding="utf-8", newline="")
        touched.append(str(path))
    return touched


def revert(paths: list[str]) -> None:
    if paths:
        run(["git", "checkout", "--", *paths], check=False)


# --------------------------------------------------------------------------- report


def pct(x: float) -> str:
    return f"{x:.2f}"


def render_report(ctx: Context, outcome: str, triage: dict, analysis: Optional[dict], test_tail: str = "") -> str:
    icon = {"fixed": "✅", "human": "🧑‍💻", "other": "ℹ️"}[outcome]
    title = {
        "fixed": "Renovate agentic remediation fix",
        "human": "Breaking change needs human review",
        "other": "Failure not caused by a dependency API change",
    }[outcome]
    lines = [
        COMMENT_MARKER,
        f"## {icon} {title}",
        "",
        f"**Dependency:** {bumps_text(ctx)}  ",
        f"**Failure log:** {ctx.log_source}",
        "",
        "### Triage (Jev)",
        "| Decision | Result | Confidence | Gate |",
        "|---|---|---|---|",
        f"| Failure Classification | `{triage['category']}` | {pct(triage['category_confidence'])} | "
        f"{'pass' if triage['category_ok'] else 'fail'} (≥ {pct(triage['threshold'])}, `API_BREAK`) |",
    ]
    if "fixable" in triage:
        lines.append(
            f"| is_fixable | `{str(triage['fixable'] >= 0.5).lower()}` | {pct(triage['fixable'])} | "
            f"{'pass' if triage['fixable'] >= triage['threshold'] else 'fail'} (≥ {pct(triage['threshold'])}) |"
        )
    if analysis:
        lines += [
            "",
            f"### Root cause (Claude `{CLAUDE_MODEL}`, self-reported confidence {pct(analysis['confidence'])})",
            analysis["root_cause"],
            "",
            "**Upstream breaking change:** " + analysis["breaking_change"],
            "",
            "**Fix:** " + analysis["fix_summary"],
        ]
        if analysis["edits"]:
            lines += ["", "<details><summary>Proposed edits</summary>", ""]
            for e in analysis["edits"]:
                diff = "\n".join(
                    [f"--- a/{e['file']}", f"+++ b/{e['file']}"]
                    + [f"-{ln}" for ln in e["old_string"].splitlines()]
                    + [f"+{ln}" for ln in e["new_string"].splitlines()]
                )
                lines += ["```diff", diff, "```"]
            lines += ["</details>"]
    if triage.get("verification"):
        lines += ["", f"### Verification\n{triage['verification']}"]
    if test_tail:
        lines += ["", "<details><summary>Test output (tail)</summary>", "", "```", test_tail, "```", "</details>"]
    lines += ["", *render_timeline()]
    return "\n".join(lines)


def render_timeline() -> list[str]:
    rows = [
        "<details><summary>Agent timeline (tokens and cost per step)</summary>",
        "",
        "| Step | Actor | Result | Duration | Tokens in / out | Cost (USD) |",
        "|---|---|---|---|---|---|",
    ]
    for s in TRACE.data["steps"]:
        u = s.get("usage")
        tokens = f"{u['input_tokens']:,} / {u['output_tokens']:,}" if u else "–"
        cost = f"{u['cost_usd']:.5f}" if u else "–"
        result = s.get("error") or s.get("decision", "")
        rows.append(f"| {s['step']} | {s['actor']} | {result} | {s.get('duration_s', 0):.1f}s | {tokens} | {cost} |")
    t = TRACE.totals()
    rows += [
        f"| **Total** | | | | **{t['input_tokens']:,} / {t['output_tokens']:,}** | **{t['cost_usd']:.5f}** |",
        "",
        "Full trace (Jev inputs and answers, Claude's step-by-step investigation): "
        "`remediation-trace.json` in the workflow run's artifacts.",
        "</details>",
    ]
    return rows


def publish(ctx: Context, report: str, args: argparse.Namespace) -> None:
    Path(args.report).write_text(report, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    if ctx.pr and not args.dry_run:
        run(["gh", "pr", "comment", str(ctx.pr), "-R", ctx.repo, "--body-file", args.report])
        log(f"commented on PR #{ctx.pr}")


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="owner/name; required with --pr")
    ap.add_argument("--pr", type=int, help="PR number. The PR branch must be checked out.")
    ap.add_argument("--base", default="origin/poc-baseline", help="base ref for local diff (no --pr)")
    ap.add_argument("--log-file", help="use this failure log instead of CI / local run")
    ap.add_argument("--local-log", action="store_true", help="reproduce locally even if CI logs exist")
    python = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
    ap.add_argument("--test-cmd", default=f"{python} -m pytest -q -p no:cacheprovider --tb=short")
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--dry-run", action="store_true", help="don't commit, push, or comment")
    ap.add_argument("--report", default="remediation-report.md")
    ap.add_argument("--trace", default="remediation-trace.json", help="step-by-step trace output")
    args = ap.parse_args()
    if args.pr and not args.repo:
        ap.error("--repo is required with --pr")

    run_info: dict[str, Any] = {"repo": args.repo, "pr": args.pr, "threshold": args.threshold,
                                "dry_run": args.dry_run, "outcome": "error"}
    try:
        run_info["outcome"] = remediate(args)
        return 0
    finally:
        TRACE.write(args.trace, **run_info)
        t = TRACE.totals()
        log(f"trace written to {args.trace}: {t['input_tokens']} in / {t['output_tokens']} out tokens, "
            f"${t['cost_usd']:.5f}")


def remediate(args: argparse.Namespace) -> str:
    """Run the flow; returns the outcome recorded in the trace."""
    with TRACE.step("Collect context", "Script") as step:
        ctx = collect_context(args)
        step.update(
            branch=ctx.branch,
            failure_log_source=ctx.log_source,
            dependency_change=bumps_text(ctx),
            release_notes_source=ctx.release_notes.splitlines()[0] if ctx.release_notes else "",
            decision=f"{bumps_text(ctx)}; log from {ctx.log_source}",
        )
    log(f"branch={ctx.branch} bumps={bumps_text(ctx)} log={ctx.log_source}")

    # Step 1: Jev classification
    category, conf = jev_classify(ctx)
    triage = {"category": category, "category_confidence": conf, "threshold": args.threshold,
              "category_ok": category == "API_BREAK" and conf >= args.threshold}
    if not triage["category_ok"]:
        triage["verification"] = "Skipped: classification gate not met; no code changes attempted."
        outcome = "other" if category == "OTHER" else "human"
        publish(ctx, render_report(ctx, outcome, triage, None), args)
        return outcome

    # Step 2: Claude root cause + proposed edits
    analysis = claude_analyze(ctx)

    # Step 3: Jev fixability
    triage["fixable"] = jev_fixable(ctx, analysis) if analysis["edits"] else 0.0
    if triage["fixable"] < args.threshold:
        triage["verification"] = "Skipped: is_fixable gate not met; no code changes applied."
        publish(ctx, render_report(ctx, "human", triage, analysis), args)
        return "human"

    # Step 4: apply + verify (quality gate)
    with TRACE.step("Apply edits", "Script") as step:
        try:
            touched = apply_edits(analysis["edits"])
            step.update(files=touched, decision=f"edited {', '.join(touched)}")
        except (ValueError, OSError) as err:
            step["decision"] = f"failed: {err}"
            touched = None
    if touched is None:
        triage["verification"] = f"Could not apply edits: {step['decision']}. Reverted."
        revert([e["file"] for e in analysis["edits"]])
        publish(ctx, render_report(ctx, "human", triage, analysis), args)
        return "human"

    log(f"applied edits to {touched}; running tests")
    with TRACE.step("Run test suite", "Script") as step:
        tests = run(args.test_cmd, check=False)
        summary = re.findall(r"^=*\s*(\d+ (?:passed|failed|error).*?in [\d.]+s)", tests.stdout, re.M)
        step.update(command=args.test_cmd, exit_code=tests.returncode,
                    decision=summary[-1] if summary else f"exit code {tests.returncode}")
    tail = clip((tests.stdout if tests.returncode == 0 else tests.stdout + tests.stderr).strip(), 2500, keep="tail")
    if tests.returncode != 0:
        revert(touched)
        triage["verification"] = "❌ Tests still fail after applying the fix. Changes reverted."
        publish(ctx, render_report(ctx, "human", triage, analysis, tail), args)
        return "human"

    triage["verification"] = f"✅ Full test suite passed: `{step['decision']}`"
    if not args.dry_run:
        with TRACE.step("Commit and push", "Script") as step:
            run(["git", "add", *touched])
            msg = (
                f"fix: adapt to {bumps_text(ctx)} breaking change\n\n{analysis['fix_summary']}\n\n"
                f"Triage: Jev {category} ({pct(conf)}), is_fixable {pct(triage['fixable'])}.\n"
                f"Automated by tools/remediation/remediate.py"
            )
            run(["git", "-c", "user.name=remediation-bot",
                 "-c", "user.email=remediation-bot@users.noreply.github.com", "commit", "-m", msg])
            run(["git", "push", "origin", f"HEAD:{ctx.branch}"])
            sha = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
            step.update(commit=sha, decision=f"pushed {sha} to {ctx.branch}")
        triage["verification"] += f"\n\nPushed fix as {sha} to `{ctx.branch}`."
    publish(ctx, render_report(ctx, "fixed", triage, analysis, tail), args)
    return "fixed"


if __name__ == "__main__":
    sys.exit(main())
