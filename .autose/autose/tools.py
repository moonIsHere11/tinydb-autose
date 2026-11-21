"""Per-role tools + the network-less sandbox (ADR-005, ADR-009).

Capability profiles: a role only gets the tools in PROFILES[role], and write
tools are path-scoped.  `run` executes inside `docker run --network none` with
an empty environment: no API key, no GitHub token, no network.  Even a fully
prompt-injected agent can only change files in its scope, which the gate
re-checks on the final diff.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

IMAGE = "autose-sandbox"
HIDDEN = (".git/", ".github/", ".autose/")

PROFILES = {
    "analyst": {"tools": {"list", "read", "grep", "finish"}, "write": ()},
    "builder": {"tools": {"list", "read", "grep", "write", "replace", "run", "finish"}, "write": ("tinydb/",)},
    "tester": {"tools": {"list", "read", "grep", "write", "replace", "run", "finish"}, "write": ("tests/autose/",)},
    "reviewer": {"tools": {"list", "read", "grep", "run", "finish"}, "write": ()},
}
PROFILES["reviewer_naive"] = PROFILES["reviewer"]

MAX_OBS = 6000


def sandbox(repo: Path, cmd: str, readonly: bool = False, timeout: int = 150,
            scratch: Path | None = None) -> tuple[int, str]:
    """Run a shell command in a throwaway container. Returns (exit_code, output).
    `scratch` (if given) is mounted at /scratch and survives across one agent's commands."""
    mount = f"{repo.resolve()}:/work" + (":ro" if readonly else "")
    extra = ["-v", f"{scratch.resolve()}:/scratch"] if scratch else []
    argv = ["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--pids-limit", "256",
            "--cpus", "2", "--tmpfs", "/tmp:exec,size=256m", "-u", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "COVERAGE_FILE=/tmp/.coverage",
            "-e", "PYTEST_ADDOPTS=-p no:cacheprovider -o addopts=", "-e", "MYPY_CACHE_DIR=/tmp/.mypy",
            "-v", mount, *extra, "-w", "/work", IMAGE, "timeout", str(timeout - 10), "bash", "-c", cmd]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env={"PATH": os.environ["PATH"]})
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"[sandbox] command timed out after {timeout}s"


def _clip(s: str, n: int = MAX_OBS) -> str:
    return s if len(s) <= n else s[: n // 2] + f"\n... [{len(s) - n} chars clipped] ...\n" + s[-n // 2:]


class Toolbox:
    def __init__(self, role: str, repo: Path, readonly_run: bool = False):
        self.role, self.repo, self.readonly_run = role, repo.resolve(), readonly_run
        self.prof = PROFILES[role]
        self.runs: list[dict] = []  # evidence log (commands actually executed)
        import tempfile
        self.scratch = Path(tempfile.mkdtemp(prefix=f"autose-{role}-"))

    # ---- path jail -------------------------------------------------------
    def _path(self, p: str) -> Path:
        p = (p or ".").lstrip("/").removeprefix("work/")
        full = (self.repo / p).resolve()
        if not str(full).startswith(str(self.repo)):
            raise PermissionError(f"path {p!r} escapes the repository")
        rel = str(full.relative_to(self.repo)) + ("/" if full.is_dir() else "")
        if any(rel.startswith(h) or rel + "/" == h for h in HIDDEN):
            raise PermissionError(f"path {p!r} is not visible to agents")
        return full

    def _writable(self, p: str) -> Path:
        full = self._path(p)
        rel = str(full.relative_to(self.repo))
        if not any(rel.startswith(w) for w in self.prof["write"]):
            raise PermissionError(f"role {self.role} may only write under {self.prof['write']}, not {rel}")
        return full

    # ---- tools -------------------------------------------------------------
    def call(self, tool: str, args: dict) -> str:
        if tool not in self.prof["tools"]:
            return f"ERROR: tool {tool!r} is not available to role {self.role}. Available: {sorted(self.prof['tools'])}"
        try:
            return getattr(self, "t_" + tool)(**args)
        except PermissionError as e:
            return f"PERMISSION DENIED: {e}"
        except TypeError as e:
            return f"ERROR: bad arguments for {tool}: {e}"
        except Exception as e:  # noqa: BLE001 - surface to the agent
            return f"ERROR: {type(e).__name__}: {e}"

    def t_list(self, path: str = ".") -> str:
        d = self._path(path)
        out = []
        for c in sorted(d.iterdir()):
            rel = str(c.relative_to(self.repo)) + ("/" if c.is_dir() else "")
            if any(rel.startswith(h) for h in HIDDEN) or "__pycache__" in rel:
                continue
            out.append(rel)
        return "\n".join(out) or "(empty)"

    def t_read(self, path: str, start: int = 1, end: int = 400) -> str:
        lines = self._path(path).read_text().splitlines()
        start, end = max(1, int(start)), min(len(lines), int(end))
        body = "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n[file has {len(lines)} lines; showing {start}-{end}]" if end < len(lines) or start > 1 else ""
        return _clip(body + more)

    def t_grep(self, pattern: str, path: str = ".") -> str:
        rx = re.compile(pattern)
        root = self._path(path)
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        hits = []
        for f in files:
            rel = str(f.relative_to(self.repo))
            if any(rel.startswith(h) for h in HIDDEN):
                continue
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()}")
        return _clip("\n".join(hits[:200]) or "(no matches)")

    def t_write(self, path: str, content: str) -> str:
        f = self._writable(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content if content.endswith("\n") else content + "\n")
        return f"wrote {path} ({len(content.splitlines())} lines)"

    def t_replace(self, path: str, old: str, new: str) -> str:
        f = self._writable(path)
        src = f.read_text()
        n = src.count(old)
        if n != 1:
            return f"ERROR: `old` must occur exactly once in {path}, found {n} times. Read the file and copy the exact text."
        f.write_text(src.replace(old, new))
        return f"replaced 1 occurrence in {path}"

    def t_run(self, cmd: str) -> str:
        code, out = sandbox(self.repo, cmd, readonly=self.readonly_run, scratch=self.scratch)
        self.runs.append({"cmd": cmd, "exit": code, "out_tail": out[-1500:]})
        return _clip(f"$ {cmd}\n[exit {code}]\n{out}")

    def t_finish(self, **kw) -> str:  # handled by the agent loop
        return "finished"
