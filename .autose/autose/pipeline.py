"""AutoSE pipeline entry point. One sub-command per Actions job (ADR-002).

The harness (this file), never an LLM, decides stage transitions and back-edges:
  requirements -> design -> implementation -> testing -> (release, review in later jobs)
  testing --code failure--> implementation (<=3 attempts) --exhausted--> design (once) --> stop
  testing --vacuous/missing tests--> tester rewrite (<=2)
  review --approach--> design ; review --code--> implementation  (rework job, bounded by the DAG)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import gates
from .agent import run_agent
from .llm import BudgetExceeded

HERE = Path(__file__).resolve().parent.parent
PROMPTS = HERE / "prompts"
TOKEN_BUDGET = int(os.environ.get("AUTOSE_TOKEN_BUDGET", "2500000"))
STEPS = int(os.environ.get("AUTOSE_MAX_STEPS", "16"))


def prompt(name: str, **kw) -> str:
    t = (PROMPTS / f"{name}.md").read_text()
    for k, v in kw.items():
        t = t.replace("{" + k + "}", str(v))
    return t


def untrusted(issue: dict) -> str:
    body = (issue.get("body") or "").replace("</untrusted_issue>", "")
    return f"<untrusted_issue>\nTitle: {issue.get('title', '')}\n\n{body}\n</untrusted_issue>"


class Run:
    """Holds a job's working state and writes artifacts as it goes."""

    def __init__(self, repo: Path, out: Path, issue: dict):
        self.repo, self.out, self.issue = repo, out, issue
        out.mkdir(parents=True, exist_ok=True)
        (out / "transcripts").mkdir(exist_ok=True)
        self.usage: dict = {}
        self.events: list[dict] = []
        self.status: dict = {"ok": False, "stages": {}, "stopped_at": None, "reason": None}
        self.base = gates.git(repo, "rev-parse", "HEAD").strip()
        self.test_file = f"tests/autose/test_issue_{issue['number']}.py"

    def log(self, stage: str, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {stage}: {msg}"
        print(line, flush=True)
        self.events.append({"t": time.time(), "stage": stage, "msg": msg[:2000]})

    def stage(self, name: str, ok: bool, report: str, attempts: int):
        self.status["stages"][name] = {"ok": ok, "report": report[:1500], "attempts": attempts}

    def save(self):
        (self.out / "status.json").write_text(json.dumps(self.status, indent=2))
        (self.out / "usage.json").write_text(json.dumps(self.usage, indent=2))
        (self.out / "events.json").write_text(json.dumps(self.events, indent=2))

    def stop(self, stage: str, reason: str, artifact: str):
        self.status.update(ok=False, stopped_at=stage, reason=reason[:3000])
        p = self.out / artifact
        prev = p.read_text() if p.exists() else ""
        p.write_text(prev + f"\n\n---\n## STOPPED at stage `{stage}`\nThe stage check could not be passed within its budget.\n\n```\n{reason[:4000]}\n```\n")
        self.save()
        self.log(stage, "STOP: " + reason[:300])

    def agent(self, role, system, task, tag, finish_args, readonly=False):
        res, tb = run_agent(role, system, task, self.repo, self.usage, self.out / "transcripts" / f"{tag}.jsonl",
                            finish_args, max_steps=STEPS, token_budget=TOKEN_BUDGET, readonly_run=readonly)
        return res, tb


# ------------------------------------------------------------------ stages
def do_requirements(r: Run) -> bool:
    fb = ""
    for attempt in range(1, 4):
        task = (f"Issue #{r.issue['number']}:\n{untrusted(r.issue)}\n\nWrite the spec." +
                (f"\n\nYour previous spec was rejected by the stage check:\n{fb}\nFix it." if fb else ""))
        res, _ = r.agent("analyst", prompt("analyst_spec"), task, f"1-requirements-{attempt}", "--- content (the spec markdown)")
        spec = (res or {}).get("content", "")
        ok, rep = gates.gate_spec(spec)
        (r.out / "spec.md").write_text(spec + f"\n\n---\n**Stage check (code):** {rep}\n")
        r.log("requirements", f"attempt {attempt}: {rep[:200]}")
        if ok:
            r.stage("requirements", True, rep, attempt)
            r.spec = spec
            return True
        fb = rep if res else "You ran out of steps without calling finish. Call finish earlier."
    r.stage("requirements", False, fb, 3)
    r.stop("requirements", fb, "spec.md")
    return False


def do_design(r: Run, extra: str = "") -> bool:
    fb = ""
    for attempt in range(1, 4):
        task = (f"Spec:\n{r.spec}\n\nWrite the design." +
                (f"\n\nThe previous design did not work out. Reason:\n{extra}\nPropose a different, better design." if extra else "") +
                (f"\n\nYour previous design was rejected by the stage check:\n{fb}\nFix it." if fb else ""))
        res, _ = r.agent("analyst", prompt("analyst_design"), task, f"2-design-{len(r.status['stages'])}-{attempt}", "--- content (the design markdown)")
        design = (res or {}).get("content", "")
        ok, rep = gates.gate_design(design, r.repo)
        (r.out / "design.md").write_text(design + f"\n\n---\n**Stage check (code):** {rep}\n")
        r.log("design", f"attempt {attempt}: {rep[:200]}")
        if ok:
            r.stage("design", True, rep, attempt)
            r.design = design
            return True
        fb = rep if res else "You ran out of steps without calling finish. Call finish earlier."
    r.stage("design", False, fb, 3)
    r.stop("design", fb, "design.md")
    return False


def reset_code(r: Run):
    subprocess.run(["git", "-C", str(r.repo), "checkout", "--", "tinydb"], check=True)
    subprocess.run(["git", "-C", str(r.repo), "clean", "-fdq", "tinydb"], check=True)


def do_build_and_test(r: Run, feedback: str = "", allow_redesign: bool = True) -> bool:
    impl_attempts, test_attempts, redesigned = 0, 0, False
    need_tests = not (r.repo / r.test_file).exists()
    while True:
        # ---- stage 3: implementation
        impl_attempts += 1
        if impl_attempts > 3:
            if allow_redesign and not redesigned:
                redesigned = True
                r.log("implementation", "3 attempts failed -> back to DESIGN")
                reset_code(r)
                if not do_design(r, extra=feedback):
                    return False
                impl_attempts, feedback = 1, ""
            else:
                r.stage("implementation", False, feedback, impl_attempts - 1)
                r.stop("testing", "Implementation could not satisfy the tests within budget:\n" + feedback, "test_report.md")
                return False
        src = gates.symbol_source(r.repo, gates.parse_changes(r.design))
        task = (f"Spec:\n{r.spec}\n\nDesign:\n{r.design}\n\n"
                + (f"Current source of the symbols the design names (already read for you):\n{src}\n\n" if src else "")
                + "Implement the design now: make the edit within your first few steps, then verify." +
                (f"\n\nFEEDBACK from the previous attempt (fix this):\n{feedback}" if feedback else ""))
        res, _ = r.agent("builder", prompt("builder"), task, f"3-implementation-{impl_attempts}{'-r' if redesigned else ''}", "--- summary")
        ok, rep = gates.gate_build(r.repo)
        r.log("implementation", f"attempt {impl_attempts}: {rep[:200]}")
        (r.out / "builder_summary.md").write_text((res or {}).get("summary", "(no summary: step budget exhausted)"))
        r.stage("implementation", ok, rep, impl_attempts)
        if not ok:
            feedback = rep
            continue
        # ---- stage 4: testing
        tfb = ""
        while True:
            if need_tests:
                test_attempts += 1
                task = (f"Spec:\n{r.spec}\n\nWrite {r.test_file}." +
                        (f"\n\nYour previous tests were rejected by the stage check:\n{tfb}\nFix the tests." if tfb else ""))
                r.agent("tester", prompt("tester", test_file=r.test_file), task, f"4-testing-{test_attempts}", "--- summary")
                need_tests = False
            ok, rep, info = gates.gate_tests(r.repo, r.base, r.test_file, r.acs, r.out / "work")
            r.log("testing", rep[:300])
            with (r.out / "test_evals.jsonl").open("a") as fh:  # every evaluation, for the E2 replay
                fh.write(json.dumps({"ok": ok, "report": rep, "table": info.get("table", ""),
                                     "test_attempt": test_attempts, "impl_attempt": impl_attempts}) + "\n")
            report = (f"# Test report: issue #{r.issue['number']}\n\n**Stage check (code):** {rep}\n\n## Red/green per acceptance criterion\n"
                      f"{info.get('table', '')}\n\n## Full suite on head\n```\n{info.get('suite', '')}\n```\n")
            (r.out / "test_report.md").write_text(report)
            if ok:
                r.stage("testing", True, rep, test_attempts)
                return True
            test_fault = any(k in rep for k in ("vacuous", "has no test named", "was not written", "syntax error"))
            if test_fault and test_attempts < 3:
                tfb, need_tests = rep, True
                continue
            if test_fault:
                r.stage("testing", False, rep, test_attempts)
                r.stop("testing", rep, "test_report.md")
                return False
            feedback = rep + "\n\nNew test output on head:\n" + info.get("head_out", "")[-2000:]
            break  # back to implementation


def write_patch(r: Run):
    subprocess.run(["git", "-C", str(r.repo), "add", "-A", "tinydb", "tests/autose"], check=True)
    patch = gates.git(r.repo, "diff", "--cached", r.base, "--", "tinydb", "tests/autose")
    (r.out / "diff.patch").write_text(patch)
    return patch


def finish_run(r: Run) -> int:
    if r.status.get("stopped_at") is None:
        write_patch(r)
        r.status["ok"] = True
    r.save()
    return 0


# ------------------------------------------------------------------ commands
def cmd_build(a):
    issue = json.loads(Path(a.issue_file).read_text())
    r = Run(Path(a.repo), Path(a.out), issue)
    try:
        if do_requirements(r):
            r.acs = gates.parse_acs(r.spec)
            if do_design(r):
                do_build_and_test(r)
    except BudgetExceeded as e:
        r.stop("budget", str(e), "spec.md")
    return finish_run(r)


def cmd_rework(a):
    """Round k>=1: re-enter at design (approach findings) or implementation (code findings)."""
    issue = json.loads(Path(a.issue_file).read_text())
    prev, rev = Path(a.prev), json.loads(Path(a.review).read_text())
    r = Run(Path(a.repo), Path(a.out), issue)
    r.spec = (prev / "spec.md").read_text().split("\n\n---\n**Stage check")[0]
    r.design = (prev / "design.md").read_text().split("\n\n---\n**Stage check")[0]
    r.acs = gates.parse_acs(r.spec)
    shutil.copy(prev / "spec.md", r.out / "spec.md")
    shutil.copy(prev / "design.md", r.out / "design.md")
    patch = (prev / "diff.patch").read_text()
    if patch.strip():
        subprocess.run(["git", "-C", str(r.repo), "apply", "--whitespace=nowarn", "-"], input=patch, text=True, check=True)
    findings = "\n".join(f"- [{f.get('category')}] {f.get('file', '')}: {f.get('issue')} (evidence: {f.get('evidence')})" for f in rev["findings"])
    approach = any(f.get("category") == "approach" for f in rev["findings"])
    r.status["entered_at"] = "design" if approach else "implementation"
    r.log("rework", f"round {a.round}: re-entering at {r.status['entered_at']} with {len(rev['findings'])} finding(s)")
    try:
        if approach:
            reset_code(r)
            if do_design(r, extra="Independent review found:\n" + findings):
                do_build_and_test(r, allow_redesign=False)
        else:
            do_build_and_test(r, feedback="Independent reviewer findings:\n" + findings, allow_redesign=False)
    except BudgetExceeded as e:
        r.stop("budget", str(e), "test_report.md")
    return finish_run(r)


def parse_findings(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-* ").strip()
        if not line or line.lower() == "none":
            continue
        f = {}
        for part in line.split(" | "):
            if ":" in part:
                k, v = part.split(":", 1)
                f[k.strip().lower()] = v.strip()
        if f.get("issue"):
            f["category"] = "approach" if "approach" in f.get("category", "") else "code"
            out.append(f)
    return out


def _review_guard(args: dict, tb) -> str | None:
    """Evidence gate enforced inside the loop: no approval before the reviewer has executed anything."""
    v = (args.get("verdict") or "").lower()
    if not any(k in v for k in ("approve", "changes_requested", "changes requested")):
        return ("your finish has no `verdict:` header line. Call finish again with a line "
                "`verdict: approve` or `verdict: changes_requested`, plus the summary and findings sections.")
    if "approve" in v and not tb.runs:
        return ("you have not executed any command. Before approving, run the test suite and at least one probe "
                "script in /tmp that tries to break the change, then decide again.")
    return None


def cmd_review(a):
    """Stage 6. Inputs: issue + diff (computed here from git, not taken from the builder) + repo at PR head."""
    repo, out = Path(a.repo), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    issue = json.loads(Path(a.issue_file).read_text())
    diff = gates.git(repo, "diff", f"{a.base}...HEAD", "--", ".", ":(exclude).autose", ":(exclude).github")
    usage: dict = {}
    if a.variant == "naive":
        # E1 variant: the reviewer sees the author's own account of the change (PR description / spec / design)
        ctx = Path(a.context)
        parts = [f"{name}:\n{(ctx / fn).read_text()}" for name, fn in (("Author's PR description", "pr_body.md"),
                 ("Specification", "spec.md"), ("Design", "design.md"), ("Author's summary", "builder_summary.md"))
                 if (ctx / fn).exists()]
        task = "\n\n".join(parts) + f"\n\nDiff:\n```diff\n{diff[:30000]}\n```"
        role, system = "reviewer_naive", prompt("reviewer_naive")
    else:
        task = f"Issue #{issue['number']}:\n{untrusted(issue)}\n\nDiff under review:\n```diff\n{diff[:30000]}\n```"
        role, system = "reviewer", prompt("reviewer")
    from .agent import run_agent as ra
    res, tb = ra(role, system, task, repo, usage, out / "transcripts" / "6-review.jsonl",
                 "verdict: approve|changes_requested, --- summary, --- findings", max_steps=STEPS,
                 token_budget=TOKEN_BUDGET, readonly_run=True,
                 finish_guard=None if a.variant == "naive" else _review_guard)
    res = res or {"verdict": "none", "summary": "Reviewer exhausted its step budget without a verdict."}
    raw = {"verdict": res.get("verdict", ""), "summary": res.get("summary", ""), "findings": parse_findings(res.get("findings", ""))}
    if a.variant == "naive":
        verdict, kept, note = (raw["verdict"].strip().lower().replace(" ", "_"), raw["findings"], "naive variant: no evidence gate")
    else:
        verdict, kept, note = gates.gate_review(raw, tb.runs, repo)
    rows = "\n".join(f"| {i} | {f['category']} | `{f.get('file', '')}` | {f['issue']} | {f.get('evidence', '')[:400]} |" for i, f in enumerate(kept, 1))
    from .llm import ROLE_MODELS
    used = sorted(usage.get("by_model", {}))
    primary = ROLE_MODELS[role][0]
    degraded = bool(used) and used != [primary]
    model_line = (f"\n\n**Reviewer model:** {', '.join(used) or 'none'}"
                  + (f" — **degraded**: primary `{primary}` was unavailable; treat this review with extra caution." if degraded else ""))
    md = (f"## AutoSE independent review (round {a.round}, {a.variant} reviewer)\n\n**Verdict:** `{verdict}`"
          f"{'  — ' + note if note else ''}{model_line}\n\n{raw['summary']}\n\n"
          + (f"| # | category | file | finding | evidence |\n|---|---|---|---|---|\n{rows}\n\n" if kept else "")
          + f"<details><summary>Commands the reviewer executed ({len(tb.runs)})</summary>\n\n"
          + "\n".join(f"- `{x['cmd'][:150]}` → exit {x['exit']}" for x in tb.runs) + "\n</details>\n\n"
          + "_Reviewer inputs: issue text, diff, repository at PR head. Not shown: spec, design, implementer transcript._\n")
    (out / "review.md").write_text(md)
    (out / "review.json").write_text(json.dumps({"verdict": verdict, "raw_verdict": raw["verdict"], "findings": kept,
                                                 "note": note, "runs": tb.runs, "usage": usage}, indent=2))
    print(md)
    gh_out(verdict=verdict, approach=str(any(f["category"] == "approach" for f in kept)).lower())
    return 0


# ------------------------------------------------------------------ GitHub side (deterministic, holds the token)
def gh(*args, input=None, check=True) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {p.stderr[:500]}")
    return p.stdout


def gh_out(**kv):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


def cmd_ship(a):
    """Stage 5 (release). Not an agent: apply patch, push branch, open/update PR, run CI, set commit status."""
    repo, work, out = Path(a.repo), Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    issue = json.loads(Path(a.issue_file).read_text())
    n, rp = issue["number"], os.environ["GITHUB_REPOSITORY"]
    branch = f"autose/issue-{n}"
    status = json.loads((work / "status.json").read_text())
    if not status.get("ok"):
        (out / "pr.md").write_text(f"Release skipped: build stopped at `{status.get('stopped_at')}`.\n")
        gh_out(shipped="false")
        return 0
    g = lambda *x, **k: subprocess.run(["git", "-C", str(repo), *x], check=True, **k)
    g("config", "user.name", "autose-bot")
    g("config", "user.email", "autose-bot@users.noreply.github.com")
    if a.round == 0:
        g("checkout", "-B", branch)
    else:
        g("fetch", "origin", branch)
        g("checkout", "-B", branch, "FETCH_HEAD")
        g("reset", "--hard", a.base)  # re-apply full patch from base: rework patch is cumulative
    g("apply", "--index", "--whitespace=nowarn", str(work / "diff.patch"))
    g("commit", "-m", f"autose: issue #{n} (round {a.round})\n\nGenerated by the AutoSE workflow run {os.environ.get('GITHUB_RUN_ID')}.")
    g("push", "-f", "origin", f"HEAD:refs/heads/{branch}")
    head = gates.git(repo, "rev-parse", "HEAD").strip()
    # pr.md from a template: acceptance criteria + test evidence, no agent reasoning (ADR-003)
    spec = (work / "spec.md").read_text()
    acs = "\n".join(f"- **AC-{x['id']}** {x['title']}" for x in gates.parse_acs(spec))
    tr = (work / "test_report.md").read_text().split("## Red/green per acceptance criterion")[-1]
    run_url = f"{os.environ.get('GITHUB_SERVER_URL')}/{rp}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
    body = (f"Closes #{n} (on merge, by a human).\n\nAutomated change produced by the AutoSE workflow ([run]({run_url})).\n\n"
            f"### Acceptance criteria\n{acs}\n\n### Red/green evidence{tr}\n\n"
            f"Artifacts (spec.md, design.md, diff.patch, test_report.md) are attached to the run. "
            f"An independent reviewer will post its review here. **This workflow never merges; a maintainer decides.**\n")
    (out / "pr.md").write_text(body)
    existing = gh("pr", "list", "-R", rp, "--head", branch, "--state", "open", "--json", "number", "-q", ".[0].number").strip()
    if existing:
        pr = existing
        gh("pr", "edit", pr, "-R", rp, "--body-file", str(out / "pr.md"))
    else:
        url = gh("pr", "create", "-R", rp, "--base", a.base_branch, "--head", branch,
                 "--title", f"[autose] {issue['title'][:80]} (#{n})", "--body-file", str(out / "pr.md")).strip()
        pr = url.rstrip("/").split("/")[-1]
    # CI on the pushed head (GITHUB_TOKEN pushes do not trigger other workflows, so CI runs here)
    from .tools import sandbox
    rc, ci_out = sandbox(repo, "python -m pytest -q tests && mypy tinydb", timeout=400)
    (out / "ci.log").write_text(ci_out)
    state = "success" if rc == 0 else "failure"
    gh("api", "-X", "POST", f"repos/{rp}/statuses/{head}", "-f", f"state={state}", "-f", "context=autose/ci",
       "-f", f"description=pytest + mypy on PR head: {state}", "-f", f"target_url={run_url}")
    tail = "\n".join(ci_out.strip().splitlines()[-2:])
    (out / "pr.md").write_text(body + f"\n### CI (stage 5 check)\n`{state}`: {tail}\n\nPR: {os.environ.get('GITHUB_SERVER_URL')}/{rp}/pull/{pr}\n")
    gh_out(shipped="true" if rc == 0 else "false", pr=pr, head=head, ci=state)
    print(f"PR #{pr} head {head} CI {state}")
    return 0


def cmd_post_review(a):
    rp = os.environ["GITHUB_REPOSITORY"]
    gh("pr", "review", a.pr, "-R", rp, "--comment", "--body-file", a.review_md)
    return 0


# ---- run ledger: one comment on the issue, re-rendered by every job (ADR-004)
MARK = "<!-- autose-ledger -->"
ORDER = ["trigger", "requirements", "design", "implementation", "testing", "release", "review_1", "rework_1",
         "release_1", "review_2", "final"]


def cmd_ledger(a):
    rp = os.environ["GITHUB_REPOSITORY"]
    comments = json.loads(gh("api", f"repos/{rp}/issues/{a.issue}/comments", "--paginate") or "[]")
    mine = [c for c in comments if MARK in c["body"] and c["user"]["login"] == "github-actions[bot]"]
    state = {}
    if mine and a.fresh != "true":
        m = re.search(r"<!-- autose-state (.*?) -->", mine[-1]["body"], re.S)
        state = json.loads(m.group(1)) if m else {}
    run_url = f"{os.environ.get('GITHUB_SERVER_URL')}/{rp}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
    for item in a.set or []:
        k, val = item.split("=", 1)
        status, _, note = val.partition(":")
        state[k] = {"status": status, "note": note[:300], "run": run_url}
    if a.status_json and Path(a.status_json).exists():
        s = json.loads(Path(a.status_json).read_text())
        for k, v in s.get("stages", {}).items():
            state[k] = {"status": "pass" if v["ok"] else "fail", "note": f"{v['attempts']} attempt(s): " + v["report"].splitlines()[0][:200], "run": run_url}
    icon = {"pass": "✅", "fail": "❌", "running": "⏳", "skip": "➖", "approve": "✅", "changes_requested": "🔁", "stop": "🛑"}
    rows = "\n".join(f"| {k} | {icon.get(v['status'], '•')} {v['status']} | {v['note']} | [run]({v['run']}) |"
                     for k, v in sorted(state.items(), key=lambda kv: ORDER.index(kv[0]) if kv[0] in ORDER else 99))
    body = (f"{MARK}\n### AutoSE run ledger\n| stage | status | check / note | link |\n|---|---|---|---|\n{rows}\n\n"
            f"_Updated by the workflow after each job. The workflow never merges; a maintainer decides._\n"
            f"<!-- autose-state {json.dumps(state)} -->")
    if mine and a.fresh != "true":
        gh("api", "-X", "PATCH", f"repos/{rp}/issues/comments/{mine[-1]['id']}", "-f", f"body={body}")
    else:
        gh("api", "-X", "POST", f"repos/{rp}/issues/{a.issue}/comments", "-f", f"body={body}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="autose")
    sp = p.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build"); b.add_argument("--issue-file"); b.add_argument("--out"); b.add_argument("--repo", default=".")
    rw = sp.add_parser("rework"); rw.add_argument("--issue-file"); rw.add_argument("--prev"); rw.add_argument("--review")
    rw.add_argument("--round", type=int); rw.add_argument("--out"); rw.add_argument("--repo", default=".")
    rv = sp.add_parser("review"); rv.add_argument("--issue-file"); rv.add_argument("--base"); rv.add_argument("--out")
    rv.add_argument("--round", type=int, default=1); rv.add_argument("--variant", default="full"); rv.add_argument("--context")
    rv.add_argument("--repo", default=".")
    s = sp.add_parser("ship"); s.add_argument("--issue-file"); s.add_argument("--work"); s.add_argument("--out")
    s.add_argument("--round", type=int, default=0); s.add_argument("--base"); s.add_argument("--base-branch", default="master")
    s.add_argument("--repo", default=".")
    pr = sp.add_parser("post-review"); pr.add_argument("--pr"); pr.add_argument("--review-md")
    lg = sp.add_parser("ledger"); lg.add_argument("--issue"); lg.add_argument("--set", action="append")
    lg.add_argument("--status-json"); lg.add_argument("--fresh", default="false")
    a = p.parse_args(argv)
    return {"build": cmd_build, "rework": cmd_rework, "review": cmd_review, "ship": cmd_ship,
            "post-review": cmd_post_review, "ledger": cmd_ledger}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
