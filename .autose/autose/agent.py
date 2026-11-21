"""ReAct-style agent loop with a plain-text action protocol.

The model answers with brief reasoning and exactly one block:

    <action>
    tool: replace
    path: tinydb/table.py
    --- old
    <exact old text>
    --- new
    <new text>
    </action>

Scalar args are `key: value` header lines; multi-line args are `--- name`
sections.  No JSON escaping, so large code edits survive weaker models.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .llm import LLM
from .tools import Toolbox

PROTOCOL = """\
## How to act
Each reply: at most a few sentences of reasoning, then EXACTLY ONE action block:

<action>
tool: <tool name>
<arg>: <single-line value>
--- <multi-line arg name>
<multi-line value, copied exactly>
</action>

Tools available to you: {tools}
- list   path            -> directory listing
- read   path, optional start/end -> numbered lines (default 1-400), e.g. header lines `path: tinydb/table.py`, `start: 230`, `end: 280`
- grep   pattern [path]  -> regex search over .py files
- write  path, --- content       -> create/overwrite a file (only inside your write scope: {scope})
- replace path, --- old, --- new -> replace an exact, unique snippet (copy `old` verbatim, without line numbers)
- run    --- cmd         -> shell command in an offline sandbox at the repo root (python, pytest, mypy available)
- finish {finish_args}
You get the tool result as the next message. Do not invent tool results. Stop with `finish` when done.
"""


def parse_action(text: str) -> dict | None:
    m = re.search(r"<action>\s*\n(.*?)\n\s*</action>", text, re.S)
    if not m:
        return None
    body = m.group(1)
    args: dict = {}
    body = re.sub(r"^--- (\w+):[ \t]+(\S.*)$", r"\1: \2", body, flags=re.M)  # inline "--- cmd: x" form
    head, *secs = re.split(r"^--- (\w+)[ \t]*$", body, flags=re.M)
    for line in head.splitlines():
        rng = re.match(r"^\s*(?:lines?\s*:?\s*)?(\d+)\s*(?:-|to|,|\s)\s*(\d+)\s*$", line)
        if rng:  # weaker models write "230 280" or "lines: 230-280" for a range
            args["start"], args["end"] = rng.group(1), rng.group(2)
        elif ":" in line:
            k, v = line.split(":", 1)
            args[k.strip().lstrip("-").strip()] = v.strip()
    for name, val in zip(secs[0::2], secs[1::2]):
        args[name] = val.strip("\n")
    tool = args.pop("tool", None)
    if not tool:
        return None
    for k in ("start", "end"):
        if k in args:
            try:
                args[k] = int(args[k])
            except ValueError:
                args.pop(k)
    return {"tool": tool, "args": args}


def run_agent(role: str, system: str, task: str, repo: Path, usage: dict, log: Path,
              finish_args: str, max_steps: int = 15, token_budget: int = 1_500_000,
              readonly_run: bool = False, finish_guard=None) -> tuple[dict | None, Toolbox]:
    """Returns (finish_args_dict or None if step budget exhausted, toolbox)."""
    tb = Toolbox(role, repo, readonly_run=readonly_run)
    llm = LLM(role, usage, token_budget)
    sysmsg = system + "\n\n" + PROTOCOL.format(
        tools=", ".join(sorted(tb.prof["tools"])), scope=", ".join(tb.prof["write"]) or "none (read-only)",
        finish_args=finish_args)
    msgs = [{"role": "user", "text": task}]
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as lf:
        lf.write(json.dumps({"event": "start", "role": role, "time": time.time()}) + "\n")
        for step in range(1, max_steps + 1):
            # keep context bounded: shrink old observations, keep the last 4 intact
            for m in msgs[1:-8]:
                if m["role"] == "user" and len(m["text"]) > 600:
                    m["text"] = m["text"][:600] + "\n[older observation truncated]"
            text, model = llm.chat(sysmsg, msgs)
            act = parse_action(text)
            lf.write(json.dumps({"step": step, "model": model, "reply": text[:8000], "action": act}) + "\n")
            msgs.append({"role": "model", "text": text})
            if act is None:
                obs = "FORMAT ERROR: no valid <action> ... </action> block found. Reply again with exactly one action block."
            elif act["tool"] == "finish":
                refusal = finish_guard(act["args"], tb) if finish_guard else None
                if refusal is None or step == max_steps:
                    lf.write(json.dumps({"event": "finish", "step": step}) + "\n")
                    return act["args"], tb
                obs = "FINISH REFUSED: " + refusal
            else:
                obs = tb.call(act["tool"], act["args"])
            left = max_steps - step
            note = f"\n[{left} steps left]" + (" You must call finish NOW." if left <= 1 else "")
            msgs.append({"role": "user", "text": f"OBSERVATION:\n{obs}{note}"})
            lf.write(json.dumps({"step": step, "observation": obs[:4000]}) + "\n")
    return None, tb
