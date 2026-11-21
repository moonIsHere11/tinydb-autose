"""Stage checks. Code, not an LLM, decides pass/fail for every stage (ADR-007).

Each gate returns (ok: bool, report: str). The report is fed back to the
agent on retry and written into the stage artifact on a stop.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

from .tools import sandbox

FUZZY = ["fast", "quick", "efficient", "easy", "simple", "robust", "user-friendly", "intuitive",
         "appropriate", "reasonable", "properly", "correctly", "nice", "better", "seamless", "etc"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout


# ---------------------------------------------------------------- stage 1
def parse_acs(spec: str) -> list[dict]:
    acs = []
    for m in re.finditer(r"^#{2,4}\s*AC-(\d+)\b(.*)$", spec, re.M):
        start = m.end()
        nxt = re.search(r"^#{1,4}\s", spec[start:], re.M)
        body = spec[start: start + nxt.start()] if nxt else spec[start:]
        acs.append({"id": int(m.group(1)), "title": m.group(2).strip(" :-"), "body": body,
                    "regression": "[regression-only]" in m.group(2).lower() or "[regression-only]" in body.lower()})
    return acs


def gate_spec(spec: str) -> tuple[bool, str]:
    acs = parse_acs(spec)
    problems = []
    if not acs:
        problems.append("No acceptance criteria found. Each must be a heading `### AC-<n>: <title>`.")
    ids = [a["id"] for a in acs]
    if len(set(ids)) != len(ids):
        problems.append(f"Duplicate AC ids: {ids}")
    if len(acs) > 6:
        problems.append(f"{len(acs)} ACs; keep the change small (at most 6).")
    for a in acs:
        b = a["body"]
        for kw in ("Given", "When", "Then"):
            if not re.search(rf"\b{kw}\b", b):
                problems.append(f"AC-{a['id']}: missing `{kw}` clause.")
        then = " ".join(re.findall(r"\bThen\b(.*)", b))
        fz = [w for w in FUZZY if re.search(rf"\b{re.escape(w)}\b", then, re.I)]
        if fz:
            problems.append(f"AC-{a['id']}: `Then` uses untestable words {fz}; state an observable value/state.")
    ok = not problems
    return ok, ("PASS: %d testable ACs (%s)" % (len(acs), ", ".join(f"AC-{i}" for i in ids))) if ok else "FAIL:\n- " + "\n- ".join(problems)


# ---------------------------------------------------------------- stage 2
def _symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add(f"{node.name}.{sub.name}")
    return out


def symbol_source(repo: Path, rows: list[tuple[str, str, str]], limit: int = 260) -> str:
    """Source (with line numbers) of every existing symbol the design names: the Builder's starting context."""
    out, used = [], 0
    for f, sym, kind in rows:
        p = repo / f
        if not p.exists() or not sym:
            continue
        lines = p.read_text().splitlines()
        tree = ast.parse("\n".join(lines))
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and "." in sym and node.name == sym.split(".")[0]:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == sym.split(".")[1]:
                        target = sub
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == sym and "." not in sym:
                target = node
        if target is None:
            continue
        a, b = target.lineno, target.end_lineno or target.lineno
        if used + (b - a) > limit:
            break
        used += b - a
        out.append(f"### {f}::{sym} (lines {a}-{b})\n```python\n" +
                   "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(a, b + 1)) + "\n```")
    return "\n\n".join(out)


def parse_changes(design: str) -> list[tuple[str, str, str]]:
    rows = []
    for line in design.splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.match(r"^(tinydb|tests)/[\w/]+\.py$", cells[0]):
            rows.append((cells[0], cells[1].removesuffix("()"), cells[2].upper()))
    return rows


def gate_design(design: str, repo: Path) -> tuple[bool, str]:
    rows = parse_changes(design)
    problems = []
    if not rows:
        problems.append("No Changes table found: rows must look like `| tinydb/<file>.py | <Class.method or function> | MODIFY or NEW |`.")
    if rows and not any(r[0].startswith("tinydb/") for r in rows):
        problems.append("The plan changes no file under tinydb/.")
    for f, sym, kind in rows:
        p = repo / f
        if "NEW" not in kind and not p.exists():
            problems.append(f"{f} does not exist (mark the row NEW if the file is new).")
            continue
        if "MODIFY" in kind and p.exists() and sym and sym not in _symbols(p):
            problems.append(f"{f} has no symbol `{sym}` (use Class.method; mark NEW if you add it).")
    ok = not problems
    return ok, (f"PASS: plan names {len(rows)} existing/new symbols: " + "; ".join(f"{f}::{s} ({k})" for f, s, k in rows)) if ok else "FAIL:\n- " + "\n- ".join(problems)


# ---------------------------------------------------------------- stage 3
def changed_files(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain", "--untracked-files=all")
    files = []
    for line in out.splitlines():
        f = line[3:].split(" -> ")[-1].strip('"')
        if "__pycache__" not in f and not f.startswith(".pytest_cache"):
            files.append(f)
    return files


def gate_build(repo: Path) -> tuple[bool, str]:
    files = changed_files(repo)
    code = [f for f in files if f.startswith("tinydb/")]
    bad = [f for f in files if not (f.startswith("tinydb/") or f.startswith("tests/autose/"))]
    problems = []
    if not code:
        problems.append("No change under tinydb/.")
    if bad:
        problems.append(f"Changes outside the allowed scope: {bad}")
    rc, out = sandbox(repo, "python -m compileall -q tinydb && python -c 'import tinydb' && mypy tinydb")
    if rc != 0:
        problems.append("Build/type check failed (compileall, import, mypy):\n" + out[-3000:])
    ok = not problems
    return ok, (f"PASS: builds, imports and type-checks; changed {code}") if ok else "FAIL:\n- " + "\n- ".join(problems)


# ---------------------------------------------------------------- stage 4
_RES = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)", re.M)


def _results(out: str) -> dict[str, str]:
    res = {}
    for st, node in _RES.findall(out):
        res[node.split(" - ")[0]] = st
    return res


def gate_tests(repo: Path, base: str, test_file: str, acs: list[dict], workdir: Path) -> tuple[bool, str, dict]:
    """Traceability + RED on base / GREEN on head + full suite green on head."""
    problems, info = [], {}
    tf = repo / test_file
    if not tf.exists():
        return False, f"FAIL:\n- {test_file} was not written.", info
    try:
        names = [n.name for n in ast.walk(ast.parse(tf.read_text())) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    except SyntaxError as e:
        return False, f"FAIL:\n- {test_file} has a syntax error: {e}", info
    by_ac = {a["id"]: [n for n in names if re.match(rf"test_ac{a['id']}(_|$)", n)] for a in acs}
    for a in acs:
        if not by_ac[a["id"]]:
            problems.append(f"AC-{a['id']} has no test named `test_ac{a['id']}_...`.")

    # GREEN on head: new tests
    rc_h, out_h = sandbox(repo, f"python -m pytest -rA -q {test_file}")
    head = _results(out_h)
    # RED on base: same test file against the base commit
    base_dir = workdir / "base_tree"
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)
    subprocess.run(f"git -C {repo} archive {base} tinydb tests pytest.ini | tar -x -C {base_dir}", shell=True, check=True)
    (base_dir / test_file).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(tf, base_dir / test_file)
    rc_b, out_b = sandbox(base_dir, f"python -m pytest -rA -q {test_file}")
    basr = _results(out_b)
    collect_error_on_base = rc_b in (2, 4) or bool(re.search(r"^ERROR\s+\S+\.py\b", out_b, re.M)) and not basr
    table = ["| AC | test | base (expect fail) | head (expect pass) |", "|---|---|---|---|"]
    for a in acs:
        for n in by_ac[a["id"]]:
            node = f"{test_file}::{n}"
            h = head.get(node, "MISSING")
            b = "ERROR(collection)" if collect_error_on_base else basr.get(node, "MISSING")
            table.append(f"| AC-{a['id']} | `{n}` | {b} | {h} |")
            if h != "PASSED":
                problems.append(f"{n} does not pass on head ({h}).")
        if by_ac[a["id"]] and not a["regression"]:
            red = collect_error_on_base or any(basr.get(f"{test_file}::{n}") in ("FAILED", "ERROR") for n in by_ac[a["id"]])
            if not red:
                problems.append(f"AC-{a['id']}: every test for it already PASSES on the unchanged base code, so it does not "
                                "verify the change (vacuous test). Make it exercise the new/fixed behaviour.")
    # full suite
    rc_s, out_s = sandbox(repo, "python -m pytest -q tests", timeout=300)
    tail = "\n".join(out_s.strip().splitlines()[-3:])
    if rc_s != 0:
        problems.append("Full test suite is not green on head:\n" + out_s[-2500:])
    info = {"table": "\n".join(table), "suite": tail, "head_out": out_h[-3000:], "base_out": out_b[-2000:]}
    ok = not problems
    return ok, ("PASS: every AC has a test that fails on base and passes on head; full suite green (" + tail.splitlines()[-1] + ")") if ok else "FAIL:\n- " + "\n- ".join(problems), info


# ---------------------------------------------------------------- stage 6
def gate_review(verdict: dict, runs: list[dict]) -> tuple[str, list[dict], str]:
    """Evidence gate. Returns (verdict, kept_findings, note)."""
    v = (verdict.get("verdict") or "").strip().lower().replace(" ", "_")
    findings = verdict.get("findings") or []
    kept = [f for f in findings if str(f.get("evidence", "")).strip()]
    dropped = len(findings) - len(kept)
    note = []
    if dropped:
        note.append(f"{dropped} finding(s) without evidence were dropped by the evidence gate.")
    if v not in ("approve", "changes_requested"):
        return "invalid", kept, f"verdict {v!r} is not approve|changes_requested"
    if v == "approve" and not runs:
        return "invalid", kept, "approval without executing any command is rejected by the evidence gate"
    if v == "changes_requested" and not kept:
        return "approve_by_default", kept, "changes requested but no finding carried evidence"
    return v, kept, " ".join(note)
