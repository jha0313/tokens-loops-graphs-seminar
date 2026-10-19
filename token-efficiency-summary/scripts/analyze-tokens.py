#!/usr/bin/env python3
"""Read-only token analyzer for Claude Code session transcripts.

Reads ~/.claude/projects/<project-dir>/<session-id>.jsonl and subagent
transcripts in <project-dir>/<session-id>/subagents/*.jsonl. Counts tokens for
every model call, runs 23 fixed-rule waste detectors (21 by default, 23 with
--full), measures cache economics (no-cache counterfactual, mid-session cache
breaks by cause, a 1-hour TTL simulation) and prints JSON. Python 3.8+
standard library only.

It never writes to a transcript. Files are opened read-only. It writes only
two kinds of file, and only when asked: the history file (--save-history), at
$TOKEN_EFFICIENCY_HISTORY or ~/.claude/token-efficiency/history.jsonl, and the
HTML dashboard (--dashboard <path>).
"""

import argparse
import glob
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

VERSION = "1.1.0"

# --- Prices --------------------------------------------------------------------
# USD per 1M tokens. Source: Anthropic public list prices (pricing page and the
# claude-api skill), read on 2026-10-17.
# write_5m = 5-minute cache write (1.25 x input), write_1h = 1-hour cache write
# (2 x input). Each call is priced by the TTL that its usage block reports
# (cache_creation.ephemeral_1h_input_tokens); unlabelled writes count as 5-minute.
# Haiku 5.5 prompts over 100K tokens use the long-context row.
# Not modelled: batch and fast mode.
# The first substring that matches the model name wins (specific before family).
_PRICE_ROWS = [
    # substring,   input, output, cache_read, write_5m, write_1h
    ("fable-5-1", 10.00, 50.00, 0.25, 12.50, 20.00),
    ("mythos-5-1", 10.00, 50.00, 0.25, 12.50, 20.00),
    ("fable", 10.00, 50.00, 1.00, 12.50, 20.00),     # Fable 5
    ("mythos", 10.00, 50.00, 1.00, 12.50, 20.00),    # Mythos 5
    ("opus-5-5", 4.00, 20.00, 0.20, 5.00, 8.00),
    ("opus", 5.00, 25.00, 0.50, 6.25, 10.00),        # Opus 5, 4.x
    ("sonnet-5-5", 2.00, 10.00, 0.10, 2.50, 4.00),
    ("sonnet-4", 3.00, 15.00, 0.30, 3.75, 6.00),     # Sonnet 4.x
    ("sonnet", 2.00, 10.00, 0.20, 2.50, 4.00),       # Sonnet 5
    ("haiku-4", 1.00, 5.00, 0.10, 1.25, 2.00),       # Haiku 4.5
    ("haiku", 0.10, 0.50, 0.01, 0.125, 0.20),        # Haiku 5.5, prompts up to 100K tokens
]
DEFAULT_PRICE_KEY = "sonnet"
PRICES = {}
for _k, _i, _o, _r, _w5, _w1 in _PRICE_ROWS:
    PRICES[_k] = {"input": _i, "output": _o, "cache_read": _r, "cache_create": _w5, "cache_create_1h": _w1}
HAIKU_LONG_MIN = 100000
HAIKU_LONG = {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_create": 0.625, "cache_create_1h": 1.00}

COST_METHOD = ("list price per model call, price row chosen by model-name substring; "
               "cache writes priced by their TTL (5-minute 1.25 x input, 1-hour 2 x input); "
               "Haiku 5.5 prompts over 100K tokens use the long-context row")

# --- Thresholds (spec A.7) -------------------------------------------------------
CHARS_PER_TOKEN = 4
CTX_GROWTH_MIN_TURNS = 20
CTX_GROWTH_MIN_CONTEXT = 100000
COMPACTION_DROP = 0.5            # a drop of more than 50% between two calls
LARGE_RESULT_CHARS = 50000
CACHE_MISS_RATIO = 0.5
CACHE_MISS_MIN_CONTEXT = 30000
SPRAWL_MIN_TURNS = 5
SPRAWL_MAX_AVG_GAP = 3
GHOST_MIN_CONTEXT = 50000
GHOST_MAX_TOOL_CALLS = 2
LONG_SESSION_TURNS = 30
LONG_SESSION_CACHE_WRITE = 50000
SUBAGENT_WASTE_MIN = 3
SPAWN_GROWTH = 1.5
NEAR_DUP_OVERLAP = 0.5
LONG_PROMPT_CHARS = 5000
EDIT_CHURN_MIN = 3
READ_AFTER_EDIT_TURNS = 2
OVERSIZED_READ_CHARS = 20000
BINARY_RESULT_TOKENS = 1600      # about one full-size image; used for dollars only
THRASH_MIN = 3
THRASH_WINDOW_TURNS = 8
EXPLORATION_MIN = 15
REWORK_TURNS = 5
REWORK_MIN = 2
MISMATCH_MIN = 5
MISMATCH_TARGET = "sonnet-5-5"   # model_mismatch dollars = output price gap to this row
IDLE_GAP_SECONDS = 600
IDLE_MIN_CONTEXT = 30000
IDLE_MIN_GAPS = 3
# system_prompt_bloat counts only the part of the prefix a run can drop: the
# skill listing, MCP instructions, deferred (MCP) tool names and agent listing
# that Claude Code records as attachments. The built-in prompt, built-in tools
# and CLAUDE.md are not counted. For old transcripts without attachment
# records, the fallback is system tokens - BUILTIN_SYSTEM_TOKENS.
SYSTEM_BLOAT_OPTIONAL_TOKENS = 5000
BUILTIN_SYSTEM_TOKENS = 22000    # lean headless runs, CLI 2.1.30x: 21,875-26,258 measured 2026-10-17
OPTIONAL_PREFIX = ("skill_listing", "deferred_tools_delta", "mcp_instructions_delta", "agent_listing_delta")
THINKING_CHARS = 2000
LENGTH_WARN_TURNS = 50
LENGTH_WARN_CONTEXT = 120000
# A "simple tool-dispatch turn": every call in the turn uses a tool, with less
# than this much visible text and tool input.
SIMPLE_TEXT_CHARS = 200
SIMPLE_INPUT_CHARS = 1000
DEFAULT_READ_LINES = 2000

# Cache breaks: a call that reads less than half of the previous context from
# cache and writes more than BREAK_MIN_WRITE tokens.
BREAK_READ_RATIO = 0.5
BREAK_MIN_WRITE = 20000
TTL_5M_SECONDS = 300
TTL_1H_SECONDS = 3600

READ_TOOLS = {"Read"}
EXPLORE_TOOLS = {"Read", "Grep", "Glob"}
# Read-only shell commands that count as exploration. Each command in a
# chain (&&, ||, ;, newline) counts once; a pipeline counts once.
EXPLORE_BASH = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "ls", "tree", "cat", "head", "tail",
                "less", "more", "sed", "awk", "wc", "nl", "file", "stat", "jq"}
EXPLORE_GIT = {"log", "show", "grep", "blame", "ls-files", "diff"}
SHELL_SPLIT = re.compile(r"&&|\|\||;|\n")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
AGENT_TOOLS = {"Agent", "Task"}
READ_SEARCH_WORDS = re.compile(
    r"\b(search|find|explor|read|look|locat|investigat|research|grep|scan|survey|gather|"
    r"collect|fact-?check|inventor|map)", re.I)
TAG_BLOCK = re.compile(r"<([A-Za-z][\w:.-]*)\b[^>]*>.*?</\1\s*>", re.S)

RECOMMENDATIONS = {
    "context_growth_no_compaction": "Compact or start a fresh session before the context passes 100K tokens. "
                                    "Split long work into short child tasks.",
    "large_tool_result": "Limit tool output: read with offset/limit, grep before cat, and send long "
                         "command output to head or a file.",
    "cache_miss_spike": "Keep the prompt prefix stable in a session: do not switch model, tools or mode "
                        "mid-session, and resume before the cache expires.",
    "redundant_tool_call": "Do not repeat an identical call. Reuse the earlier result.",
    "subagent_sprawl": "Launch fewer subagents. Put related questions into one task.",
    "ghost_trace": "Stop or downgrade scheduled and headless runs that do almost no work.",
    "session_too_long": "Start a new session for each task. Carry over a short summary, not the full history.",
    "subagent_model_waste": "Set a cheaper model (haiku or sonnet) for search and read subagents.",
    "recursive_spawn_risk": "Cap the number of subagents per step. Do not let subagents fan out further.",
    "near_duplicate_read": "Read each file range once. Keep the lines you need in notes.",
    "wasted_call_error": "Fix the cause of failing calls (wrong path, bad command, missing tool) before you retry.",
    "long_prompt": "Move long instructions into a file or CLAUDE.md and refer to it.",
    "edit_churn": "Plan the change, then make one complete edit per file.",
    "read_after_edit": "Trust the edit result. Do not read a file again right after you edit it.",
    "oversized_read": "Read big files with offset/limit, or grep for the part you need.",
    "tool_thrashing": "Choose one approach. Do not switch back and forth between tools.",
    "exploration_overhead": "Name exact files (file:line) in the prompt, or document the layout in CLAUDE.md.",
    "post_compaction_rework": "Before compaction, write key facts to a notes file so that files need no re-read.",
    "model_mismatch": "Use a cheaper model for simple tool-dispatch work.",
    "idle_session_cost": "Compact or end the session before a long break. The cache expires and the "
                         "context is written again.",
    "system_prompt_bloat": "Remove unused skills, MCP servers and hooks. Shorten CLAUDE.md and skill descriptions.",
    "thinking_overhead": "Use a lower effort for simple tool-dispatch steps.",
    "session_length_warning": "Think about a fresh session: this one is long and its context is large.",
}


class AnalyzerError(Exception):
    pass


# --- Small helpers -------------------------------------------------------------------

def price_key(model):
    m = (model or "").lower()
    for key in (row[0] for row in _PRICE_ROWS):
        if key in m:
            return key
    return DEFAULT_PRICE_KEY


def call_price(model, ctx):
    """Price row for one call. Haiku 5.5 prompts over 100K tokens cost more."""
    key = price_key(model)
    if key == "haiku" and ctx > HAIKU_LONG_MIN:
        return HAIKU_LONG
    return PRICES[key]


def attachment_chars(rec, att):
    """Characters an attachment record added to the context."""
    r = rec.get("rendered")
    if isinstance(r, str):
        return len(r)
    if isinstance(r, list) and r:
        return sum(len(x) if isinstance(x, str) else len(str(x.get("text") or x.get("content") or ""))
                   if isinstance(x, dict) else len(str(x)) for x in r)
    n = 0
    for key in ("content", "addedLines", "addedBlocks", "files"):
        v = att.get(key)
        if isinstance(v, str):
            n += len(v)
        elif isinstance(v, list):
            n += sum(len(x) if isinstance(x, str) else len(json.dumps(x, ensure_ascii=False)) for x in v)
        elif isinstance(v, dict):
            n += len(json.dumps(v, ensure_ascii=False))
    return n


def bash_explore_ops(command):
    """Number of read-only exploration commands in one Bash command line."""
    n = 0
    for seg in SHELL_SPLIT.split(command or ""):
        seg = seg.split("|")[0].strip()           # a pipeline counts once, by its first command
        words = seg.split()
        while words and ENV_ASSIGN.match(words[0]):
            words.pop(0)
        if not words:
            continue
        verb = os.path.basename(words[0].lstrip("({"))
        if verb == "git":
            i = 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-C", "-c") else 1
            n += 1 if i < len(words) and words[i] in EXPLORE_GIT else 0
        elif verb == "sed" and any(w.startswith("-i") or w == "--in-place" for w in words[1:]):
            continue                              # an in-place edit, not a read
        elif verb == "cat" and re.search(r"(^|[^0-9&])>", seg):
            continue                              # cat > file writes
        elif verb in EXPLORE_BASH:
            n += 1
    return n


def model_tier(model):
    m = (model or "").lower()
    if "fable" in m or "mythos" in m:
        return 4
    if "opus" in m:
        return 3
    if "sonnet" in m:
        return 2
    if "haiku" in m:
        return 1
    return 0


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    s = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        # Python 3.8 accepts only 3 or 6 fraction digits.
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.\d+)?(.*)$", s)
        if m:
            try:
                return datetime.fromisoformat(m.group(1) + m.group(2)).timestamp()
            except ValueError:
                return None
    return None


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_tags(text):
    return TAG_BLOCK.sub("", text).strip()


def content_chars(content):
    """Character length of a message.content value."""
    if isinstance(content, str):
        return len(content)
    n = 0
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                n += len(str(b))
            elif b.get("type") == "text":
                n += len(b.get("text") or "")
            elif b.get("type") == "tool_result":
                n += len(str(b.get("content")))
            else:
                n += len(str(b))
    return n


def is_binary(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("image", "document") for b in content)


def text_lines(content):
    if isinstance(content, str):
        return content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    if isinstance(content, list):
        return sum(text_lines(b.get("text") or "") for b in content
                   if isinstance(b, dict) and b.get("type") == "text")
    return 0


def tok(chars):
    return int(chars // CHARS_PER_TOKEN)


def short(value, n=80):
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def to_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --- Parsing (spec A.5) ----------------------------------------------------------------

def parse_transcript(path):
    """One streaming pass over a transcript. Returns a session dict."""
    turns = []
    calls = []
    calls_by_id = {}
    title = None
    first_prompt = None
    cwd = None
    first_ts = last_ts = None
    last_call = None
    # Prefix attachments (skill listing, MCP and tool listings, CLAUDE.md), in characters.
    # optional_chars and by_kind restart at a compaction, which drops the old ones.
    prefix = {"attachments_seen": False, "optional_chars": 0, "by_kind": {}, "injected": 0}

    def new_turn(ts, chars=0):
        t = {"n": len(turns) + 1, "ts": ts, "chars": chars, "prompt": "", "prompt_chars": 0,
             "injected": False, "compact": False, "results": [], "calls": [],
             "gap_before": None, "prev_call": last_call}
        if last_ts is not None and ts is not None:
            t["gap_before"] = ts - last_ts
        turns.append(t)
        return t

    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            if b'"user"' not in raw and b'"assistant"' not in raw and b"title" not in raw \
                    and b'"summary"' not in raw and b'"attachment"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "attachment":
                att = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
                kind = att.get("type")
                prefix["attachments_seen"] = True
                if kind in OPTIONAL_PREFIX:
                    n = attachment_chars(rec, att)
                    prefix["optional_chars"] += n
                    prefix["injected"] += n
                    prefix["by_kind"][kind] = prefix["by_kind"].get(kind, 0) + n
                continue
            if rtype == "ai-title" and rec.get("aiTitle"):
                title = rec.get("aiTitle")
                continue
            if rtype == "summary" and rec.get("summary"):
                title = rec.get("summary")
                continue
            if rtype not in ("user", "assistant"):
                continue
            ts = parse_ts(rec.get("timestamp"))
            if cwd is None and rec.get("cwd"):
                cwd = rec.get("cwd")
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}

            if rtype == "user":
                content = msg.get("content")
                turn = new_turn(ts, content_chars(content))
                turn["injected"] = bool(rec.get("isMeta"))   # text added by Claude Code, not typed by the user
                turn["compact"] = bool(rec.get("isCompactSummary"))
                if turn["compact"]:
                    prefix["optional_chars"], prefix["by_kind"] = 0, {}
                texts = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text":
                            texts.append(b.get("text") or "")
                        elif b.get("type") == "tool_result":
                            c = b.get("content")
                            turn["results"].append({
                                "id": b.get("tool_use_id"), "turn": turn["n"], "chars": len(str(c)),
                                "is_error": bool(b.get("is_error")), "binary": is_binary(c),
                                "lines": text_lines(c), "num_lines": None, "resolved_model": None})
                prompt = strip_tags("\n".join(texts))
                turn["prompt"] = prompt
                turn["prompt_chars"] = len(prompt)
                tur = rec.get("toolUseResult")
                if isinstance(tur, dict) and len(turn["results"]) == 1:
                    f = tur.get("file")
                    if isinstance(f, dict) and isinstance(f.get("numLines"), int):
                        turn["results"][0]["num_lines"] = f.get("numLines")
                    if isinstance(tur.get("resolvedModel"), str):
                        turn["results"][0]["resolved_model"] = tur.get("resolvedModel")
                if first_prompt is None and prompt and not turn["injected"] and not turn["compact"]:
                    first_prompt = prompt
            else:
                model = msg.get("model") or ""
                if model == "<synthetic>":
                    continue
                if not turns:
                    new_turn(ts)
                mid = msg.get("id") or rec.get("uuid") or "call-%d" % len(calls)
                call = calls_by_id.get(mid)
                if call is None:
                    call = {"id": mid, "turn": turns[-1]["n"], "model": model, "ts": ts,
                            "usage": {}, "blocks": [], "keys": set(),
                            "optional_chars": prefix["optional_chars"], "optional_by_kind": dict(prefix["by_kind"])}
                    calls_by_id[mid] = call
                    calls.append(call)
                    turns[-1]["calls"].append(call)
                # Last record per message.id wins for usage (streaming chunks).
                if isinstance(msg.get("usage"), dict):
                    call["usage"] = msg["usage"]
                call["model"] = model or call["model"]
                call["ts"] = ts if ts is not None else call["ts"]
                # Each chunk carries a different content block: keep them all.
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        key = ("tool_use", b.get("id"))
                    else:
                        key = hashlib.sha1(json.dumps(b, sort_keys=True).encode("utf-8")).hexdigest()
                    if key not in call["keys"]:
                        call["keys"].add(key)
                        call["blocks"].append(b)
                last_call = call
            if ts is not None:
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)

    peak = max(calls, key=lambda c: c["optional_chars"]) if calls else \
        {"optional_chars": prefix["optional_chars"], "optional_by_kind": prefix["by_kind"]}
    pinfo = {"attachments_seen": prefix["attachments_seen"],
             "optional_tokens": tok(peak["optional_chars"]),
             "optional_by_kind": {k: tok(v) for k, v in sorted(peak["optional_by_kind"].items())},
             "optional_tokens_total": tok(prefix["injected"])}
    return _finish(path, turns, calls, title, first_prompt, cwd, first_ts, last_ts, pinfo)


def price_call(c):
    """Cost of one call by component, with its TTL mix and model row."""
    p = call_price(c["model"], c["ctx"])
    c["price"] = p
    parts = {"input": c["inp"] * p["input"], "cache_write": c["cw5"] * p["cache_create"] + c["cw1h"] * p["cache_create_1h"],
             "cache_read": c["cr"] * p["cache_read"], "output": c["out"] * p["output"]}
    c["parts"] = {k: v / 1e6 for k, v in parts.items()}
    c["cost"] = sum(c["parts"].values())
    c["nocache"] = ((c["inp"] + c["cw"] + c["cr"]) * p["input"] + c["out"] * p["output"]) / 1e6
    c["rate"] = {"input": p["input"] / 1e6, "cache_read": p["cache_read"] / 1e6, "output": p["output"] / 1e6,
                 "cache_write": (c["parts"]["cache_write"] / c["cw"]) if c["cw"] else p["cache_create"] / 1e6}


def _finish(path, turns, calls, title, first_prompt, cwd, first_ts, last_ts, pinfo=None):
    kept = []
    tool_calls = []
    by_tool_id = {}
    for c in calls:
        u = c["usage"]
        c["inp"] = to_int(u.get("input_tokens"))
        c["out"] = to_int(u.get("output_tokens"))
        c["cr"] = to_int(u.get("cache_read_input_tokens"))
        c["cw"] = to_int(u.get("cache_creation_input_tokens"))
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        c["cw1h"] = min(c["cw"], to_int(cc.get("ephemeral_1h_input_tokens")))
        c["cw5"] = c["cw"] - c["cw1h"]          # unlabelled writes count as 5-minute writes
        c["ctx"] = c["inp"] + c["cr"] + c["cw"]
        price_call(c)
        c["text_chars"] = 0
        c["thinking"] = []
        c["content_chars"] = 0
        c["tools"] = []
        for b in c["blocks"]:
            bt = b.get("type")
            if bt == "text":
                n = len(b.get("text") or "")
                c["text_chars"] += n
            elif bt == "thinking":
                n = len(b.get("thinking") or "")
                c["thinking"].append(n)
            elif bt == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                n = len(json.dumps(inp, ensure_ascii=False))
                tc = {"name": b.get("name") or "?", "input": inp, "id": b.get("id"), "turn": c["turn"],
                      "call": c, "input_chars": n, "result": None}
                c["tools"].append(tc)
                tool_calls.append(tc)
                if tc["id"]:
                    by_tool_id[tc["id"]] = tc
            else:
                n = len(str(b))
            c["content_chars"] += n
        c["est_out"] = max(tok(c["content_chars"]), c["out"])
        if c["ctx"] == 0 and c["out"] == 0 and not c["tools"]:
            continue  # an empty or failed call
        c["idx"] = len(kept)
        kept.append(c)
    kept_ids = {id(c) for c in kept}
    for t in turns:
        t["calls"] = [c for c in t["calls"] if id(c) in kept_ids]
    orphans = []
    for t in turns:
        for r in t["results"]:
            tc = by_tool_id.get(r["id"])
            if tc is not None and tc["result"] is None:
                tc["result"] = r
                r["name"] = tc["name"]
            else:
                r["name"] = "?"
                orphans.append(r)
    for i, tc in enumerate(tool_calls):
        tc["seq"] = i

    norm = path.replace(os.sep, "/")
    kind = "subagent" if "/subagents/" in norm else "session"
    parts = norm.split("/")
    if kind == "subagent":
        project_dir = parts[-4] if len(parts) >= 4 else ""
        parent = parts[-3] if len(parts) >= 3 else None
    else:
        project_dir = parts[-2] if len(parts) >= 2 else ""
        parent = None
    sid = os.path.splitext(os.path.basename(path))[0]

    models = {}
    for c in kept:
        if c["model"]:
            models[c["model"]] = models.get(c["model"], 0) + 1
    main_model = None
    if models:
        main_model = sorted(models.items(), key=lambda kv: (-kv[1], -model_tier(kv[0]), kv[0]))[0][0]

    return {"path": path, "id": sid, "kind": kind, "parent_session": parent, "project_dir": project_dir,
            "project": os.path.basename(cwd.rstrip("/")) if cwd else project_dir, "cwd": cwd,
            "title": title, "first_prompt": first_prompt, "turns": turns, "calls": kept,
            "tool_calls": tool_calls, "orphans": orphans, "first_ts": first_ts, "last_ts": last_ts,
            "models": models, "model": main_model, "price_key": price_key(main_model),
            "price": PRICES[price_key(main_model)],
            "prefix": pinfo or {"attachments_seen": False, "optional_tokens": 0, "optional_by_kind": {},
                                "optional_tokens_total": 0}}


# --- Detectors (spec A.7) ---------------------------------------------------------------

def _issue(S, signal, description, turns, tokens, waste_type="cache_create", claims=None, parts=None):
    """claims: [(call, tokens)] that the cost ledger charges; default = the first call of each turn.
    parts: cost components to charge, in order; default = the component of waste_type."""
    tokens = max(0, int(tokens))
    turns = sorted({t for t in turns if t})
    iss = {"signal": signal, "severity": SEVERITY[signal], "description": description,
           "turns": turns[:50], "tokens_wasted": tokens, "waste_type": waste_type,
           "cost_wasted_usd": round(tokens / 1e6 * S["price"][waste_type], 6),
           "recommendation": RECOMMENDATIONS[signal]}
    if claims is None:
        firsts = []
        for t in turns:
            c = _first_call_from_turn(S, t)
            if c is not None and all(c is not f for f in firsts):
                firsts.append(c)
        claims = [(c, tokens / float(len(firsts))) for c in firsts] if firsts else []
    iss["_claims"] = claims
    iss["_parts"] = parts or (WASTE_PART[waste_type],)
    return iss


WASTE_PART = {"input": "input", "cache_create": "cache_write", "cache_read": "cache_read", "output": "output"}


def _first_call_from_turn(S, turn):
    """The first call in this turn, or the first call after it."""
    for c in S["calls"]:
        if c["turn"] >= turn:
            return c
    return None


def _result_chars(tc):
    return tc["result"]["chars"] if tc["result"] else 0


def _ok(tc):
    return tc["result"] is not None and not tc["result"]["is_error"]


def _path(tc):
    return tc["input"].get("file_path") or tc["input"].get("notebook_path")


def _launches(S):
    return [tc for tc in S["tool_calls"] if tc["name"] in AGENT_TOOLS]


def _launch_tokens(tcs):
    return sum(tok(len(str(tc["input"].get("prompt") or "")) + _result_chars(tc)) for tc in tcs)


def _drops(S):
    """Indexes of calls whose context fell by more than 50% from the previous call."""
    cs = S["calls"]
    return [i for i in range(1, len(cs)) if cs[i - 1]["ctx"] > 0 and cs[i]["ctx"] < COMPACTION_DROP * cs[i - 1]["ctx"]]


def _simple_turn(turn):
    if not turn["calls"]:
        return False
    if any(not c["tools"] for c in turn["calls"]):
        return False
    text = sum(c["text_chars"] for c in turn["calls"])
    inp = sum(tc["input_chars"] for c in turn["calls"] for tc in c["tools"])
    return text < SIMPLE_TEXT_CHARS and inp < SIMPLE_INPUT_CHARS


BINARY_NOTE = " (image/PDF base64: the real token cost is much lower)"


def _kind_note(r):
    return BINARY_NOTE if r and r.get("binary") else ""


def d_context_growth(S):
    cs = S["calls"]
    if len(S["turns"]) < CTX_GROWTH_MIN_TURNS or not cs:
        return []
    peak = max(c["ctx"] for c in cs)
    if peak <= CTX_GROWTH_MIN_CONTEXT or _drops(S):
        return []
    over = [c for c in cs if c["ctx"] > CTX_GROWTH_MIN_CONTEXT]
    top = max(cs, key=lambda c: c["ctx"])
    tokens = sum(c["ctx"] - CTX_GROWTH_MIN_CONTEXT for c in over)
    desc = "Context grew from %s to %s tokens over %d turns with no compaction; %d calls ran above 100K" % (
        "{:,}".format(cs[0]["ctx"]), "{:,}".format(peak), len(S["turns"]), len(over))
    return [_issue(S, "context_growth_no_compaction", desc, [over[0]["turn"], top["turn"], cs[-1]["turn"]],
                   tokens, "cache_read", claims=[(c, c["ctx"] - CTX_GROWTH_MIN_CONTEXT) for c in over],
                   parts=("cache_read", "cache_write"))]


def d_large_tool_result(S):
    out = []
    for t in S["turns"]:
        for r in t["results"]:
            if r["chars"] > LARGE_RESULT_CHARS:
                desc = "%s result of %s chars at turn %d%s" % (r.get("name", "?"), "{:,}".format(r["chars"]),
                                                              r["turn"], _kind_note(r))
                out.append(_issue(S, "large_tool_result", desc, [r["turn"]], tok(r["chars"]),
                                  claims=_result_claims(S, r)))
    return out


def _result_claims(S, r):
    """The call that first reads a tool result pays for it. An image or PDF costs about
    BINARY_RESULT_TOKENS, not its base64 length / 4."""
    c = _first_call_from_turn(S, r["turn"])
    return [(c, BINARY_RESULT_TOKENS if r.get("binary") else tok(r["chars"]))] if c is not None else []


def d_cache_miss_spike(S):
    out = []
    prev = None
    for c in S["calls"]:
        if c["turn"] > 1 and c["ctx"] >= CACHE_MISS_MIN_CONTEXT and c["cr"] / c["ctx"] < CACHE_MISS_RATIO:
            hints = []
            if prev is not None and prev["ts"] is not None and c["ts"] is not None and c["ts"] - prev["ts"] > 300:
                hints.append("%d min after the previous call" % ((c["ts"] - prev["ts"]) // 60))
            if prev is not None and prev["model"] != c["model"]:
                hints.append("model changed from %s" % prev["model"])
            desc = "Cache hit %.0f%% on a %s-token context at turn %d; %s tokens written again%s" % (
                100.0 * c["cr"] / c["ctx"], "{:,}".format(c["ctx"]), c["turn"], "{:,}".format(c["cw"]),
                (" (" + ", ".join(hints) + ")") if hints else "")
            out.append(_issue(S, "cache_miss_spike", desc, [c["turn"]], c["cw"], claims=[(c, c["cw"])]))
        prev = c
    return out


def d_redundant_tool_call(S):
    groups = {}
    for tc in S["tool_calls"]:
        key = hashlib.sha256((tc["name"] + "\x00" + json.dumps(tc["input"], sort_keys=True)).encode("utf-8")).hexdigest()
        groups.setdefault(key, []).append(tc)
    out = []
    for tcs in groups.values():
        if len(tcs) < 2:
            continue
        tokens = sum(tok(_result_chars(tc) + tc["input_chars"]) for tc in tcs[1:])
        desc = "%s called %d times with the same input: %s" % (tcs[0]["name"], len(tcs), short(tcs[0]["input"], 70))
        out.append(_issue(S, "redundant_tool_call", desc, [tc["turn"] for tc in tcs], tokens))
    return out


def d_subagent_sprawl(S):
    launches = _launches(S)
    turns = sorted({tc["turn"] for tc in launches})
    if len(turns) < SPRAWL_MIN_TURNS:
        return []
    gaps = [b - a for a, b in zip(turns, turns[1:])]
    avg = sum(gaps) / float(len(gaps))
    if avg >= SPRAWL_MAX_AVG_GAP:
        return []
    desc = "%d agent launches in %d turns, %.1f turns apart on average" % (len(launches), len(turns), avg)
    return [_issue(S, "subagent_sprawl", desc, turns, _launch_tokens(launches))]


def d_ghost_trace(S):
    cs = S["calls"]
    if not cs:
        return []
    peak = max(c["ctx"] for c in cs)
    n = len(S["tool_calls"])
    if peak <= GHOST_MIN_CONTEXT or n > GHOST_MAX_TOOL_CALLS:
        return []
    desc = "Context reached %s tokens but the session made only %d tool calls in %d model calls" % (
        "{:,}".format(peak), n, len(cs))
    return [_issue(S, "ghost_trace", desc, [c["turn"] for c in cs], sum(c["cw"] + c["inp"] for c in cs),
                   claims=[(c, c["cw"] + c["inp"]) for c in cs], parts=("cache_write", "input"))]


def d_session_too_long(S):
    cw = sum(c["cw"] for c in S["calls"])
    if len(S["turns"]) <= LONG_SESSION_TURNS or cw <= LONG_SESSION_CACHE_WRITE:
        return []
    late = [c for c in S["calls"] if c["turn"] > LONG_SESSION_TURNS]
    desc = "%d turns and %s cache-write tokens; %s of them after turn %d" % (
        len(S["turns"]), "{:,}".format(cw), "{:,}".format(sum(c["cw"] for c in late)), LONG_SESSION_TURNS)
    # Late writes are new work, not waste. The cost of a long session is the carried context
    # (context_growth_no_compaction) and the cache breaks, so this signal charges no dollars.
    return [_issue(S, "session_too_long", desc, [LONG_SESSION_TURNS + 1, len(S["turns"])],
                   sum(c["cw"] for c in late), claims=[])]


def _cheap(tc):
    m = str(tc["input"].get("model") or "")
    if not m and tc["result"]:
        m = tc["result"].get("resolved_model") or ""
    m = m.lower()
    return "haiku" in m or "sonnet" in m


def _read_search(tc):
    if str(tc["input"].get("subagent_type") or "").lower() == "explore":
        return True
    return bool(READ_SEARCH_WORDS.search(str(tc["input"].get("description") or "")))


def d_subagent_model_waste(S):
    hits = [tc for tc in _launches(S) if _read_search(tc) and not _cheap(tc)]
    if len(hits) < SUBAGENT_WASTE_MIN:
        return []
    desc = "%d read/search agent launches with no cheaper model set (for example: %s)" % (
        len(hits), short(hits[0]["input"].get("description") or "", 50))
    return [_issue(S, "subagent_model_waste", desc, [tc["turn"] for tc in hits], _launch_tokens(hits))]


def d_recursive_spawn_risk(S):
    per_turn = {}
    for tc in _launches(S):
        per_turn.setdefault(tc["turn"], []).append(tc)
    steps = sorted(per_turn.items())
    counts = [len(v) for _t, v in steps]
    for i in range(len(counts) - 2):
        if counts[i + 1] >= SPAWN_GROWTH * counts[i] and counts[i + 2] >= SPAWN_GROWTH * counts[i + 1]:
            j = i + 2
            while j + 1 < len(counts) and counts[j + 1] >= SPAWN_GROWTH * counts[j]:
                j += 1
            run = steps[i:j + 1]
            desc = "Agent launches grew %s per step (turns %s)" % (
                " -> ".join(str(len(v)) for _t, v in run), ", ".join(str(t) for t, _v in run))
            tcs = [tc for _t, v in run for tc in v]
            return [_issue(S, "recursive_spawn_risk", desc, [t for t, _v in run], _launch_tokens(tcs))]
    return []


def _read_range(tc):
    inp = tc["input"]
    start = max(1, to_int(inp.get("offset")) or 1)
    r = tc["result"] or {}
    known = r.get("num_lines") if r.get("num_lines") is not None else (r.get("lines") or None)
    limit = to_int(inp.get("limit"))
    if limit:
        length = min(limit, known) if known else limit
    else:
        length = known or DEFAULT_READ_LINES
    return start, start + max(1, length) - 1


def d_near_duplicate_read(S):
    out = []
    seen = {}
    dup = {}
    for tc in S["tool_calls"]:
        p = _path(tc)
        if tc["name"] in EDIT_TOOLS and p and _ok(tc):
            seen[p] = []          # the file changed: earlier reads no longer count
            continue
        if tc["name"] not in READ_TOOLS or not p or not _ok(tc):
            continue
        a, b = _read_range(tc)
        best = 0.0
        for prev in seen.get(p, []):
            pa, pb = _read_range(prev)
            ov = min(b, pb) - max(a, pa) + 1
            if ov > 0:
                best = max(best, ov / float(b - a + 1))
        if best > NEAR_DUP_OVERLAP:
            dup.setdefault(p, []).append((tc, best))
        seen.setdefault(p, []).append(tc)
    for p, items in dup.items():
        tokens = sum(tok(_result_chars(tc) * frac) for tc, frac in items)
        desc = "%s read again %d time(s) with more than 50%% range overlap" % (p, len(items))
        out.append(_issue(S, "near_duplicate_read", desc, [tc["turn"] for tc, _f in items], tokens))
    return out


def d_wasted_call_error(S):
    errs = [tc for tc in S["tool_calls"] if tc["result"] and tc["result"]["is_error"]]
    if not errs:
        return []
    names = {}
    for tc in errs:
        names[tc["name"]] = names.get(tc["name"], 0) + 1
    desc = "%d tool calls returned errors (%s)" % (
        len(errs), ", ".join("%s %d" % kv for kv in sorted(names.items(), key=lambda kv: -kv[1])))
    return [_issue(S, "wasted_call_error", desc, [tc["turn"] for tc in errs],
                   sum(tok(tc["input_chars"] + _result_chars(tc)) for tc in errs))]


def d_long_prompt(S):
    out = []
    for t in S["turns"]:
        if t["injected"] or t["compact"]:
            continue
        if t["prompt_chars"] > LONG_PROMPT_CHARS:
            desc = "User prompt of %s chars (tags removed) at turn %d" % ("{:,}".format(t["prompt_chars"]), t["n"])
            out.append(_issue(S, "long_prompt", desc, [t["n"]], tok(t["prompt_chars"])))
    return out


def d_edit_churn(S):
    by_path = {}
    for tc in S["tool_calls"]:
        p = _path(tc)
        if tc["name"] in EDIT_TOOLS and p and _ok(tc):
            by_path.setdefault(p, []).append(tc)
    out = []
    for p, tcs in by_path.items():
        if len(tcs) >= EDIT_CHURN_MIN:
            desc = "%s edited %d times" % (p, len(tcs))
            out.append(_issue(S, "edit_churn", desc, [tc["turn"] for tc in tcs],
                              sum(tok(tc["input_chars"]) for tc in tcs[2:]), "output"))
    return out


def d_read_after_edit(S):
    last_edit = {}
    out = []
    for tc in S["tool_calls"]:
        p = _path(tc)
        if not p:
            continue
        if tc["name"] in EDIT_TOOLS and _ok(tc):
            last_edit[p] = tc
        elif tc["name"] in READ_TOOLS and p in last_edit:
            e = last_edit.pop(p)
            if 0 <= tc["turn"] - e["turn"] <= READ_AFTER_EDIT_TURNS:
                desc = "%s read at turn %d, %d turn(s) after an edit at turn %d" % (
                    p, tc["turn"], tc["turn"] - e["turn"], e["turn"])
                out.append(_issue(S, "read_after_edit", desc, [e["turn"], tc["turn"]], tok(_result_chars(tc))))
    return out


def d_oversized_read(S):
    out = []
    for tc in S["tool_calls"]:
        if tc["name"] not in READ_TOOLS or not tc["result"]:
            continue
        if tc["input"].get("offset") is None and tc["input"].get("limit") is None \
                and tc["result"]["chars"] > OVERSIZED_READ_CHARS:
            desc = "%s read whole: %s chars, no offset/limit%s" % (
                _path(tc), "{:,}".format(tc["result"]["chars"]), _kind_note(tc["result"]))
            out.append(_issue(S, "oversized_read", desc, [tc["turn"]], tok(tc["result"]["chars"]),
                              claims=_result_claims(S, tc["result"])))
    return out


def d_tool_thrashing(S):
    seq = []
    for c in S["calls"]:
        if c["tools"]:
            name = c["tools"][0]["name"]
            if not seq or seq[-1][1] != name:
                seq.append((c["turn"], name, c))
    triples = [(seq[i][0], seq[i + 2][0], i) for i in range(len(seq) - 2)
               if seq[i][1] == seq[i + 2][1] != seq[i + 1][1]]
    best = []
    for a in range(len(triples)):
        win = [tr for tr in triples[a:] if tr[1] - triples[a][0] < THRASH_WINDOW_TURNS]
        if len(win) > len(best):
            best = win
    if len(best) < THRASH_MIN:
        return []
    idx = sorted({i + k for _s, _e, i in best for k in range(3)})
    names = sorted({seq[i][1] for i in idx})
    tokens = sum(tok(tc["input_chars"] + _result_chars(tc)) for i in idx for tc in seq[i][2]["tools"])
    desc = "%d A->B->A tool switches (%s) within turns %d-%d" % (
        len(best), " / ".join(names), best[0][0], best[-1][1])
    return [_issue(S, "tool_thrashing", desc, [seq[i][0] for i in idx], tokens)]


def d_exploration_overhead(S):
    first_edit = None
    for tc in S["tool_calls"]:
        if tc["name"] in EDIT_TOOLS:
            first_edit = tc
            break
    if first_edit is None:
        return []   # no edit: the reads are the task, not overhead
    reads, n_tool, n_bash = [], 0, 0
    for tc in S["tool_calls"][:first_edit["seq"]]:
        if tc["name"] in EXPLORE_TOOLS:
            n_tool += 1
            reads.append(tc)
        elif tc["name"] == "Bash":
            k = bash_explore_ops(tc["input"].get("command"))
            if k:
                n_bash += k
                reads.append(tc)
    if n_tool + n_bash < EXPLORATION_MIN:
        return []
    desc = "%d exploration steps before the first edit at turn %d (Read/Grep/Glob %d, read-only shell commands %d)" % (
        n_tool + n_bash, first_edit["turn"], n_tool, n_bash)
    claims = []
    for tc in reads:
        c = _first_call_from_turn(S, tc["result"]["turn"]) if tc["result"] else None
        if c is not None:
            claims.append((c, tok(_result_chars(tc))))
    return [_issue(S, "exploration_overhead", desc, [reads[0]["turn"], first_edit["turn"]],
                   sum(tok(_result_chars(tc)) for tc in reads), claims=claims)]


def d_post_compaction_rework(S):
    out = []
    cs = S["calls"]
    for i in _drops(S):
        drop = cs[i]
        before = {_path(tc) for c in cs[:i] for tc in c["tools"] if tc["name"] in READ_TOOLS and _path(tc)}
        rereads = [tc for c in cs[i:] if c["turn"] <= drop["turn"] + REWORK_TURNS
                   for tc in c["tools"] if tc["name"] in READ_TOOLS and _path(tc) in before]
        if len(rereads) >= REWORK_MIN:
            desc = "Context fell from %s to %s tokens at turn %d, then %d files were read again within %d turns" % (
                "{:,}".format(cs[i - 1]["ctx"]), "{:,}".format(drop["ctx"]), drop["turn"], len(rereads), REWORK_TURNS)
            out.append(_issue(S, "post_compaction_rework", desc, [drop["turn"]] + [tc["turn"] for tc in rereads],
                              sum(tok(_result_chars(tc)) for tc in rereads)))
    return out


def d_model_mismatch(S):
    hits = [t for t in S["turns"] if _simple_turn(t) and any(model_tier(c["model"]) >= 3 for c in t["calls"])]
    if len(hits) < MISMATCH_MIN:
        return []
    desc = "%d simple tool-dispatch turns ran on the largest model (%s)" % (len(hits), S["model"])
    # Dollars: only the price gap to a cheaper tier (Sonnet 5.5 output), not the whole output.
    target = PRICES[MISMATCH_TARGET]["output"]
    claims = [(c, c["out"] * max(0.0, 1 - target / c["price"]["output"])) for t in hits for c in t["calls"]]
    return [_issue(S, "model_mismatch", desc, [t["n"] for t in hits],
                   sum(c["out"] for t in hits for c in t["calls"]), "output", claims=claims)]


def d_idle_session_cost(S):
    gaps = [t for t in S["turns"] if t["gap_before"] is not None and t["gap_before"] > IDLE_GAP_SECONDS
            and t["prev_call"] is not None and t["prev_call"]["ctx"] > IDLE_MIN_CONTEXT]
    if len(gaps) < IDLE_MIN_GAPS:
        return []
    desc = "%d idle gaps over 10 minutes with more than 30K context (longest %d min)" % (
        len(gaps), max(t["gap_before"] for t in gaps) // 60)
    claims = [(_first_call_from_turn(S, t["n"]), t["prev_call"]["ctx"]) for t in gaps]
    return [_issue(S, "idle_session_cost", desc, [t["n"] for t in gaps], sum(t["prev_call"]["ctx"] for t in gaps),
                   claims=[(c, n) for c, n in claims if c is not None])]


def system_tokens(S):
    """Turn 1 rule, applied at the first model call: context minus user chars / 4."""
    if not S["calls"]:
        return 0
    first = S["calls"][0]
    user_chars = sum(t["chars"] for t in S["turns"] if t["n"] <= first["turn"])
    return max(0, int(first["ctx"] - user_chars / float(CHARS_PER_TOKEN)))


def optional_prefix_tokens(S):
    """Prefix tokens a run can drop, at the first call, and how they were measured."""
    p = S["prefix"]
    if p["attachments_seen"]:
        return p["optional_tokens"], "attachments"
    return max(0, system_tokens(S) - BUILTIN_SYSTEM_TOKENS), "excess over built-in"


def d_system_prompt_bloat(S):
    if not S["calls"]:
        return []
    opt, how = optional_prefix_tokens(S)
    if opt <= SYSTEM_BLOAT_OPTIONAL_TOKENS:
        return []
    n = len(S["calls"])
    kinds = S["prefix"]["optional_by_kind"]
    parts = ", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
    desc = ("About %s optional prefix tokens at turn %d (%s; threshold %s), carried in %d calls. "
            "System tokens %s; the built-in prompt, built-in tools and CLAUDE.md are not counted") % (
        "{:,}".format(opt), S["calls"][0]["turn"], parts or how, "{:,}".format(SYSTEM_BLOAT_OPTIONAL_TOKENS), n,
        "{:,}".format(system_tokens(S)))
    if how == "attachments":
        claims = [(c, tok(c["optional_chars"])) for c in S["calls"]]
    else:
        claims = [(c, opt) for c in S["calls"]]
    return [_issue(S, "system_prompt_bloat", desc, [S["calls"][0]["turn"]], sum(t for _c, t in claims), "cache_read",
                   claims=claims, parts=("cache_read", "cache_write"))]


def d_thinking_overhead(S):
    hits = [t for t in S["turns"] if _simple_turn(t)
            and any(n > THINKING_CHARS for c in t["calls"] for n in c["thinking"])]
    if not hits:
        return []
    chars = sum(n for t in hits for c in t["calls"] for n in c["thinking"])
    desc = "%d simple tool-dispatch turns with thinking over 2,000 chars" % len(hits)
    return [_issue(S, "thinking_overhead", desc, [t["n"] for t in hits], tok(chars), "output")]


def d_session_length_warning(S):
    cs = S["calls"]
    if len(S["turns"]) <= LENGTH_WARN_TURNS or not cs or max(c["ctx"] for c in cs) <= LENGTH_WARN_CONTEXT:
        return []
    desc = "%d turns and a %s-token peak context" % (len(S["turns"]), "{:,}".format(max(c["ctx"] for c in cs)))
    return [_issue(S, "session_length_warning", desc, [LENGTH_WARN_TURNS + 1, len(S["turns"])], 0, "cache_read")]


DETECTORS = [
    ("context_growth_no_compaction", "HIGH", d_context_growth),
    ("large_tool_result", "HIGH", d_large_tool_result),
    ("cache_miss_spike", "HIGH", d_cache_miss_spike),
    ("redundant_tool_call", "HIGH", d_redundant_tool_call),
    ("subagent_sprawl", "HIGH", d_subagent_sprawl),
    ("ghost_trace", "HIGH", d_ghost_trace),
    ("session_too_long", "HIGH", d_session_too_long),
    ("subagent_model_waste", "HIGH", d_subagent_model_waste),
    ("recursive_spawn_risk", "HIGH", d_recursive_spawn_risk),
    ("near_duplicate_read", "MEDIUM", d_near_duplicate_read),
    ("wasted_call_error", "MEDIUM", d_wasted_call_error),
    ("long_prompt", "MEDIUM", d_long_prompt),
    ("edit_churn", "MEDIUM", d_edit_churn),
    ("read_after_edit", "MEDIUM", d_read_after_edit),
    ("oversized_read", "MEDIUM", d_oversized_read),
    ("tool_thrashing", "MEDIUM", d_tool_thrashing),
    ("exploration_overhead", "MEDIUM", d_exploration_overhead),
    ("post_compaction_rework", "MEDIUM", d_post_compaction_rework),
    ("model_mismatch", "MEDIUM", d_model_mismatch),
    ("idle_session_cost", "MEDIUM", d_idle_session_cost),
    ("system_prompt_bloat", "MEDIUM", d_system_prompt_bloat),
    ("thinking_overhead", "INFO", d_thinking_overhead),
    ("session_length_warning", "INFO", d_session_length_warning),
]
SEVERITY = {name: level for name, level, _f in DETECTORS}
SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "INFO": 2}


def active_detectors(full):
    return [d for d in DETECTORS if full or d[1] != "INFO"]


def run_detectors(S, full=False):
    issues = []
    for _name, _sev, fn in active_detectors(full):
        issues.extend(fn(S))
    S["breaks"] = cache_breaks(S)
    attribute_costs(S, issues, S["breaks"])
    issues.sort(key=lambda i: (-i["tokens_wasted"], SEV_ORDER[i["severity"]], i["turns"][:1]))
    return issues


# --- Cost ledger: waste dollars that never overlap and never exceed the real cost ---------

def attribute_costs(S, issues, breaks):
    """Charge each claim against what the call really cost, component by component.

    Every call starts with its real cost per component (input, cache write, cache read,
    output). Cache breaks are charged first (their cost is measured), then the detectors in
    DETECTORS order. A dollar charged once is gone, so the issues never overlap and their
    sum is at most the session cost. cost_wasted_usd is the dollars charged.
    """
    left = {id(c): dict(c["parts"]) for c in S["calls"]}

    def take(c, tokens, parts):
        got = 0.0
        for part in parts:
            rate = c["rate"][part]
            if tokens <= 0 or rate <= 0:
                continue
            g = min(tokens * rate, left[id(c)][part])
            left[id(c)][part] -= g
            got += g
            tokens -= g / rate
        return got

    for b in breaks:
        c = b.pop("_call")
        g = min(b["lost_usd"], left[id(c)]["cache_write"])
        left[id(c)]["cache_write"] -= g
        b["lost_usd"] = round(g, 6)
    order = {name: i for i, (name, _s, _f) in enumerate(DETECTORS)}
    for iss in sorted(issues, key=lambda i: order[i["signal"]]):
        claims, parts = iss.pop("_claims", []), iss.pop("_parts", ("cache_write",))
        iss["cost_wasted_usd"] = round(sum(take(c, t, parts) for c, t in claims if c is not None), 6)


# --- Cache economics -------------------------------------------------------------------------

BREAK_CAUSES = ("ttl_5m_expiry", "idle_over_1h", "prompt_changed", "compaction", "model_switch")


def cache_breaks(S):
    """Mid-session cache breaks and their extra cost (write price minus read price)."""
    out = []
    cs = S["calls"]
    turn_by_n = {t["n"]: t for t in S["turns"]}
    for prev, c in zip(cs, cs[1:]):
        if prev["ctx"] <= 0 or c["cr"] >= BREAK_READ_RATIO * prev["ctx"] or c["cw"] <= BREAK_MIN_WRITE:
            continue
        gap = (c["ts"] - prev["ts"]) if c["ts"] is not None and prev["ts"] is not None else None
        if c["model"] != prev["model"]:
            cause = "model_switch"
        elif gap is not None and gap > TTL_1H_SECONDS:
            cause = "idle_over_1h"
        elif gap is not None and gap > TTL_5M_SECONDS and c["cw5"] >= c["cw1h"]:
            cause = "ttl_5m_expiry"
        elif turn_by_n.get(c["turn"], {}).get("compact"):
            cause = "compaction"
        else:
            cause = "prompt_changed"
        p = c["price"]
        write = p["cache_create_1h"] if c["cw1h"] >= c["cw5"] else p["cache_create"]
        lost_tokens = min(c["cw"], prev["ctx"])
        out.append({"cause": cause, "turn": c["turn"], "gap_seconds": int(gap) if gap is not None else None,
                    "after_tool_use": bool(prev["tools"]), "context_before": prev["ctx"],
                    "lost_tokens": lost_tokens, "lost_usd": lost_tokens * (write - p["cache_read"]) / 1e6,
                    "_call": c, "_idx": c["idx"]})
    return out


def ttl_simulation(S):
    """Re-price this transcript as if its 5-minute cache writes were 1-hour writes.

    Breaks caused by the 5-minute TTL (gap 5-60 min) become cache reads; every other
    5-minute write costs the 1-hour price instead.
    """
    saved = {b["_idx"]: b["lost_tokens"] for b in S["breaks"] if b["cause"] == "ttl_5m_expiry"}
    actual = with_1h = extra = avoided_usd = 0.0
    for c in S["calls"]:
        p = c["price"]
        save = min(saved.get(c["idx"], 0), c["cw5"])
        w5 = c["cw5"] - save
        actual += c["cost"]
        with_1h += (c["inp"] * p["input"] + (w5 + c["cw1h"]) * p["cache_create_1h"]
                    + (c["cr"] + save) * p["cache_read"] + c["out"] * p["output"]) / 1e6
        extra += w5 * (p["cache_create_1h"] - p["cache_create"]) / 1e6
        avoided_usd += save * (p["cache_create"] - p["cache_read"]) / 1e6
    return {"actual_usd": actual, "with_1h_usd": with_1h, "delta_usd": with_1h - actual,
            "breaks_avoided": len(saved), "extra_write_usd": extra, "avoided_break_usd": avoided_usd}


def cache_economics(S):
    cs = S["calls"]
    breaks = S.get("breaks") or []
    by_cause = {}
    for b in breaks:
        e = by_cause.setdefault(b["cause"], {"count": 0, "lost_usd": 0.0, "after_tool_use": 0})
        e["count"] += 1
        e["lost_usd"] += b["lost_usd"]
        e["after_tool_use"] += 1 if b["after_tool_use"] else 0
    actual = sum(c["cost"] for c in cs)
    nocache = sum(c["nocache"] for c in cs)
    return {"actual_cost_usd": round(actual, 6), "no_cache_cost_usd": round(nocache, 6),
            "cache_savings_usd": round(nocache - actual, 6),
            "no_cache_ratio": round(nocache / actual, 3) if actual else None,
            "cache_write_5m_tokens": sum(c["cw5"] for c in cs), "cache_write_1h_tokens": sum(c["cw1h"] for c in cs),
            "session_start_write_usd": round(cs[0]["parts"]["cache_write"], 6) if cs else 0.0,
            "breaks": {"count": len(breaks), "lost_usd": round(sum(b["lost_usd"] for b in breaks), 6),
                       "by_cause": {k: dict(v, lost_usd=round(v["lost_usd"], 6)) for k, v in by_cause.items()},
                       "items": [{k: v for k, v in b.items() if not k.startswith("_")} for b in breaks]},
            "ttl_1h_simulation": {k: (round(v, 6) if isinstance(v, float) else v)
                                  for k, v in ttl_simulation(S).items()}}


# --- Report (spec A.6, A.8) -------------------------------------------------------------

def token_summary(S):
    cs = S["calls"]
    tot = {"input_tokens": sum(c["inp"] for c in cs), "output_tokens": sum(c["out"] for c in cs),
           "estimated_output_tokens": sum(c["est_out"] for c in cs),
           "cache_read_tokens": sum(c["cr"] for c in cs), "cache_write_tokens": sum(c["cw"] for c in cs),
           "cache_write_1h_tokens": sum(c["cw1h"] for c in cs), "cache_write_5m_tokens": sum(c["cw5"] for c in cs)}
    denom = tot["cache_read_tokens"] + tot["cache_write_tokens"] + tot["input_tokens"]
    tot["total_input_tokens"] = denom
    tot["cache_hit_ratio"] = round(tot["cache_read_tokens"] / float(denom), 6) if denom else 0.0
    tot["max_context_tokens"] = max([c["ctx"] for c in cs] or [0])
    tot["system_tokens"] = system_tokens(S)
    tot["optional_prefix_tokens"] = optional_prefix_tokens(S)[0] if cs else 0
    by_model = {}
    split = {"input": 0.0, "cache_write": 0.0, "cache_read": 0.0, "output": 0.0}
    for c in cs:
        key = price_key(c["model"])
        by_model[key] = by_model.get(key, 0.0) + c["cost"]
        for k in split:
            split[k] += c["parts"][k]
    tot["estimated_cost_usd"] = round(sum(c["cost"] for c in cs), 6)
    tot["cost_split_usd"] = {k: round(v, 6) for k, v in split.items()}
    tot["no_cache_cost_usd"] = round(sum(c["nocache"] for c in cs), 6)
    keys = sorted(by_model, key=lambda k: -by_model[k])
    tot["pricing_model"] = S["price_key"] if len(keys) <= 1 else "mixed: " + ", ".join(keys)
    tot["cost_by_model_usd"] = {k: round(by_model[k], 6) for k in keys}
    tot["cost_method"] = COST_METHOD
    return tot


def tool_summary(S):
    by = {}
    for tc in S["tool_calls"]:
        e = by.setdefault(tc["name"], {"calls": 0, "errors": 0, "result_chars": 0})
        e["calls"] += 1
        if tc["result"]:
            e["result_chars"] += tc["result"]["chars"]
            if tc["result"]["is_error"]:
                e["errors"] += 1
    ordered = dict(sorted(by.items(), key=lambda kv: (-kv[1]["calls"], kv[0])))
    return {"total_calls": len(S["tool_calls"]), "errors": sum(v["errors"] for v in by.values()),
            "agent_launches": len(_launches(S)), "by_tool": ordered}


def recommendations(issues, total_input):
    by = {}
    for i in issues:
        r = by.setdefault(i["signal"], {"signal": i["signal"], "severity": i["severity"],
                                        "recommendation": i["recommendation"], "issues": 0,
                                        "estimated_tokens_saved": 0, "estimated_savings_usd": 0.0})
        r["issues"] += i.get("count", 1)
        r["estimated_tokens_saved"] += i["tokens_wasted"]
        r["estimated_savings_usd"] += i["cost_wasted_usd"]
    out = []
    for r in by.values():
        r["estimated_savings_usd"] = round(r["estimated_savings_usd"], 6)
        r["estimated_savings_pct"] = round(100.0 * r["estimated_tokens_saved"] / total_input, 2) if total_input else 0.0
        out.append(r)
    out.sort(key=lambda r: (-r["estimated_tokens_saved"], SEV_ORDER[r["severity"]], r["signal"]))
    return out


def severity_counts(issues):
    c = {"HIGH": 0, "MEDIUM": 0, "INFO": 0}
    for i in issues:
        c[i["severity"]] += 1
    return c


def build_report(S, issues, full, detail=False, turn=None):
    ts = token_summary(S)
    dur = (S["last_ts"] - S["first_ts"]) if S["first_ts"] is not None and S["last_ts"] is not None else None
    session = {"id": S["id"], "kind": S["kind"], "project": S["project"], "project_dir": S["project_dir"],
               "model": S["model"], "models": S["models"], "turns": len(S["turns"]), "model_calls": len(S["calls"]),
               "first_timestamp": iso(S["first_ts"]), "last_timestamp": iso(S["last_ts"]),
               "duration_seconds": int(dur) if dur is not None else None,
               "summary": S["title"] or short(S["first_prompt"] or "", 120), "path": S["path"]}
    if S["parent_session"]:
        session["parent_session"] = S["parent_session"]
    if "breaks" not in S:
        S["breaks"] = cache_breaks(S)
        attribute_costs(S, [], S["breaks"])
    ce = cache_economics(S)
    waste = round(sum(i["cost_wasted_usd"] for i in issues), 6)
    rep = {"version": VERSION, "mode": "session", "full": bool(full),
           "detectors_run": len(active_detectors(full)), "session": session, "token_summary": ts,
           "tool_summary": tool_summary(S), "issues": issues,
           "recommendations": recommendations(issues, ts["total_input_tokens"]),
           "total_issues": len(issues), "issues_by_severity": severity_counts(issues),
           "total_cost_wasted_usd": waste,
           "waste_to_cost_ratio": round(waste / ts["estimated_cost_usd"], 3) if ts["estimated_cost_usd"] else None,
           "cache_economics": ce}
    if detail:
        rep["turns_detail"] = [turn_row(t) for t in S["turns"]]
    if turn is not None:
        rep["turn_detail"] = turn_deep_dive(S, issues, turn)
    return rep


def turn_row(t):
    cs = t["calls"]
    return {"turn": t["n"], "timestamp": iso(t["ts"]), "calls": len(cs),
            "input": sum(c["inp"] for c in cs), "output": sum(c["out"] for c in cs),
            "cache_read": sum(c["cr"] for c in cs), "cache_write": sum(c["cw"] for c in cs),
            "context": max([c["ctx"] for c in cs] or [0]), "user_chars": t["chars"],
            "tool_result_chars": sum(r["chars"] for r in t["results"]),
            "tools": [tc["name"] for c in cs for tc in c["tools"]]}


def turn_deep_dive(S, issues, n):
    if n < 1 or n > len(S["turns"]):
        raise AnalyzerError("turn %d is out of range (1-%d)" % (n, len(S["turns"])))
    t = S["turns"][n - 1]
    calls = []
    for c in t["calls"]:
        calls.append({"id": c["id"], "model": c["model"], "timestamp": iso(c["ts"]), "input": c["inp"],
                      "output": c["out"], "cache_read": c["cr"], "cache_write": c["cw"], "context": c["ctx"],
                      "cache_hit_ratio": round(c["cr"] / float(c["ctx"]), 4) if c["ctx"] else 0.0,
                      "text_chars": c["text_chars"], "thinking_chars": sum(c["thinking"]),
                      "tool_uses": [{"name": tc["name"], "input": short(tc["input"], 200),
                                     "result_chars": _result_chars(tc),
                                     "is_error": bool(tc["result"] and tc["result"]["is_error"]),
                                     "result_turn": tc["result"]["turn"] if tc["result"] else None}
                                    for tc in c["tools"]]})
    return {"turn": n, "timestamp": iso(t["ts"]), "gap_before_seconds": int(t["gap_before"]) if t["gap_before"] else None,
            "user": {"chars": t["chars"], "prompt": short(t["prompt"], 500), "is_injected": t["injected"],
                     "is_compact_summary": t["compact"],
                     "tool_results": [{"tool": r.get("name"), "chars": r["chars"], "is_error": r["is_error"],
                                       "binary": r["binary"]} for r in t["results"]]},
            "calls": calls, "row": turn_row(t), "issues": [i for i in issues if n in i["turns"]]}


def analyze_session(path, full=False, detail=False, turn=None):
    S = parse_transcript(path)
    return build_report(S, run_detectors(S, full), full, detail, turn)


# --- Discovery (spec A.4) ---------------------------------------------------------------

def norm_name(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-").lower()


def project_match(project_dir, name):
    if not name:
        return True
    d, n = norm_name(project_dir), norm_name(name)
    return d == n or d.endswith("-" + n)


def all_transcripts(root):
    sessions = glob.glob(os.path.join(root, "*", "*.jsonl"))
    subagents = glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))
    return sessions, subagents


def parse_window(text):
    m = re.match(r"^\s*(\d+)\s*([dwm])\s*$", text or "")
    if not m:
        raise AnalyzerError("bad --last value %r: use 7d, 2w or 1m" % text)
    return int(m.group(1)) * {"d": 1, "w": 7, "m": 30}[m.group(2)]


def discover_window(root, window, project):
    days = parse_window(window)
    cutoff = time.time() - days * 86400
    sessions, subagents = all_transcripts(root)

    def keep(path, depth):
        parts = path.replace(os.sep, "/").split("/")
        pdir = parts[-2] if depth == 0 else parts[-4]
        if not project_match(pdir, project):
            return False
        try:
            return os.path.getmtime(path) >= cutoff
        except OSError:
            return False
    files = [p for p in sessions if keep(p, 0)] + [p for p in subagents if keep(p, 2)]
    return sorted(files)


def find_session(root, sid):
    if os.path.isfile(sid):
        return sid
    sid = sid.strip()
    pats = [os.path.join(root, "*", sid + ".jsonl"),
            os.path.join(root, "*", "*", "subagents", sid + ".jsonl"),
            os.path.join(root, "*", "*", "subagents", "agent-" + sid + ".jsonl")]
    for pat in pats:
        hits = glob.glob(pat)
        if hits:
            return max(hits, key=os.path.getmtime)
    hits = glob.glob(os.path.join(root, "*", sid + "*.jsonl")) + \
        glob.glob(os.path.join(root, "*", "*", "subagents", sid + "*.jsonl"))
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AnalyzerError("session id %r is ambiguous: %s" % (sid, ", ".join(sorted(os.path.basename(h) for h in hits)[:10])))
    raise AnalyzerError("no transcript found for session %r under %s" % (sid, root))


def find_current(root):
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        sid = os.environ.get(var)
        if sid:
            hits = glob.glob(os.path.join(root, "*", sid + ".jsonl"))
            if hits:
                return hits[0]
    sessions, _subs = all_transcripts(root)
    if not sessions:
        raise AnalyzerError("no transcripts under %s" % root)
    return max(sessions, key=os.path.getmtime)


# --- Multi-session modes ------------------------------------------------------------------

def brief(rep):
    s, t = rep["session"], rep["token_summary"]
    top = rep["issues"][0]["signal"] if rep["issues"] else None
    return {"id": s["id"], "kind": s["kind"], "project": s["project"], "model": s["model"], "turns": s["turns"],
            "model_calls": s["model_calls"], "last_timestamp": s["last_timestamp"],
            "estimated_cost_usd": t["estimated_cost_usd"], "cache_hit_ratio": t["cache_hit_ratio"],
            "max_context_tokens": t["max_context_tokens"], "total_issues": rep["total_issues"],
            "issues_by_severity": rep["issues_by_severity"], "top_signal": top}


def _analyze_many(files, full):
    reps, errors = [], []
    for f in files:
        try:
            reps.append(analyze_session(f, full=full))
        except (OSError, AnalyzerError) as e:
            errors.append({"path": f, "error": str(e)})
    return reps, errors


def _sum_tokens(reps):
    keys = ["input_tokens", "output_tokens", "estimated_output_tokens", "cache_read_tokens",
            "cache_write_tokens", "cache_write_1h_tokens", "cache_write_5m_tokens"]
    tot = {k: sum(r["token_summary"][k] for r in reps) for k in keys}
    denom = tot["cache_read_tokens"] + tot["cache_write_tokens"] + tot["input_tokens"]
    tot["total_input_tokens"] = denom
    tot["cache_hit_ratio"] = round(tot["cache_read_tokens"] / float(denom), 6) if denom else 0.0
    tot["max_context_tokens"] = max([r["token_summary"]["max_context_tokens"] for r in reps] or [0])
    tot["estimated_cost_usd"] = round(sum(r["token_summary"]["estimated_cost_usd"] for r in reps), 6)
    tot["cost_split_usd"] = {k: round(sum(r["token_summary"]["cost_split_usd"][k] for r in reps), 6)
                             for k in ("input", "cache_write", "cache_read", "output")}
    tot["no_cache_cost_usd"] = round(sum(r["token_summary"]["no_cache_cost_usd"] for r in reps), 6)
    by_model = {}
    for r in reps:
        for k, v in r["token_summary"]["cost_by_model_usd"].items():
            by_model[k] = by_model.get(k, 0.0) + v
    tot["cost_by_model_usd"] = {k: round(v, 6) for k, v in sorted(by_model.items(), key=lambda kv: -kv[1])}
    tot["pricing_model"] = "per call, by model name"
    tot["cost_method"] = COST_METHOD
    return tot


def _counts(reps):
    n_s = sum(1 for r in reps if r["session"]["kind"] == "session")
    return {"total": len(reps), "sessions": n_s, "subagents": len(reps) - n_s}


def list_mode(files, window, project, full):
    reps, errors = _analyze_many(files, full)
    rows = sorted((brief(r) for r in reps), key=lambda b: -b["estimated_cost_usd"])
    return {"version": VERSION, "mode": "list", "window": window, "project": project, "full": bool(full),
            "transcripts": _counts(reps), "token_summary": _sum_tokens(reps), "sessions": rows, "errors": errors}


def aggregate_mode(files, window, project, full):
    reps, errors = _analyze_many(files, full)
    counts = _counts(reps)
    sig = {}
    for r in reps:
        kind = r["session"]["kind"]
        for i in r["issues"]:
            e = sig.setdefault(i["signal"], {"signal": i["signal"], "severity": i["severity"], "issues": 0,
                                             "tokens_wasted": 0, "cost_wasted_usd": 0.0,
                                             "_s": set(), "_a": set(), "_tok": {}})
            if i["signal"] in ("large_tool_result", "oversized_read"):
                e.setdefault("image_or_pdf_issues", 0)
                e.setdefault("image_or_pdf_tokens", 0)
                if BINARY_NOTE in i["description"]:
                    e["image_or_pdf_issues"] += 1
                    e["image_or_pdf_tokens"] += i["tokens_wasted"]
            e["issues"] += 1
            e["tokens_wasted"] += i["tokens_wasted"]
            e["cost_wasted_usd"] += i["cost_wasted_usd"]
            e["_s" if kind == "session" else "_a"].add(r["session"]["id"])
            e["_tok"][r["session"]["id"]] = e["_tok"].get(r["session"]["id"], 0) + i["tokens_wasted"]
    signals = []
    for e in sig.values():
        e["sessions_affected"] = len(e.pop("_s"))
        e["subagents_affected"] = len(e.pop("_a"))
        e["transcripts_affected"] = e["sessions_affected"] + e["subagents_affected"]
        e["frequency"] = "%d/%d sessions, %d/%d subagents" % (e["sessions_affected"], counts["sessions"],
                                                             e["subagents_affected"], counts["subagents"])
        tokmap = e.pop("_tok")
        e["top_transcripts"] = [k for k, _v in sorted(tokmap.items(), key=lambda kv: -kv[1])[:3]]
        e["cost_wasted_usd"] = round(e["cost_wasted_usd"], 6)
        e["recommendation"] = RECOMMENDATIONS[e["signal"]]
        signals.append(e)
    signals.sort(key=lambda e: (-e["tokens_wasted"], SEV_ORDER[e["severity"]], e["signal"]))
    tokens = _sum_tokens(reps)
    all_issues = [i for r in reps for i in r["issues"]]
    total_waste_usd = round(sum(i["cost_wasted_usd"] for i in all_issues), 6)
    recs = []
    for e in signals:
        recs.append({"signal": e["signal"], "severity": e["severity"], "recommendation": e["recommendation"],
                     "transcripts_affected": e["transcripts_affected"], "issues": e["issues"],
                     "estimated_tokens_saved": e["tokens_wasted"],
                     "estimated_savings_pct": round(100.0 * e["tokens_wasted"] / tokens["total_input_tokens"], 2)
                     if tokens["total_input_tokens"] else 0.0,
                     "estimated_savings_usd": e["cost_wasted_usd"]})
    ce = _sum_cache(reps)
    days = parse_window(window)
    acts = actions(signals, ce, days)
    return {"version": VERSION, "mode": "aggregate", "window": window, "project": project, "full": bool(full),
            "detectors_run": len(active_detectors(full)), "transcripts": counts,
            "model_calls": sum(r["session"]["model_calls"] for r in reps),
            "turns": sum(r["session"]["turns"] for r in reps),
            "token_summary": tokens, "signals": signals, "total_issues": len(all_issues),
            "issues_by_severity": severity_counts(all_issues),
            "total_tokens_wasted": sum(i["tokens_wasted"] for i in all_issues),
            "total_cost_wasted_usd": total_waste_usd,
            "waste_to_cost_ratio": round(total_waste_usd / tokens["estimated_cost_usd"], 3)
            if tokens["estimated_cost_usd"] else None,
            "recommendations": recs,
            "cache_economics": ce, "actions": acts,
            "top_actions": [a["id"] for a in acts if a["usd_30d"] > 0][:3],
            "top_transcripts": sorted((brief(r) for r in reps), key=lambda b: -b["estimated_cost_usd"])[:10],
            "errors": errors}


def _sum_cache(reps):
    sim_keys = ("actual_usd", "with_1h_usd", "delta_usd", "breaks_avoided", "extra_write_usd", "avoided_break_usd")

    def blank():
        return {"transcripts": 0, "cost_usd": 0.0, "no_cache_cost_usd": 0.0, "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0, "session_start_write_usd": 0.0, "breaks": 0, "break_lost_usd": 0.0,
                "by_cause": {}, "ttl_1h_simulation": {k: 0 for k in sim_keys}}
    out = {"all": blank(), "session": blank(), "subagent": blank()}
    for r in reps:
        c = r["cache_economics"]
        for key in ("all", r["session"]["kind"]):
            e = out[key]
            e["transcripts"] += 1
            e["cost_usd"] += c["actual_cost_usd"]
            e["no_cache_cost_usd"] += c["no_cache_cost_usd"]
            e["cache_write_5m_tokens"] += c["cache_write_5m_tokens"]
            e["cache_write_1h_tokens"] += c["cache_write_1h_tokens"]
            e["session_start_write_usd"] += c["session_start_write_usd"]
            e["breaks"] += c["breaks"]["count"]
            e["break_lost_usd"] += c["breaks"]["lost_usd"]
            for cause, v in c["breaks"]["by_cause"].items():
                d = e["by_cause"].setdefault(cause, {"count": 0, "lost_usd": 0.0, "after_tool_use": 0})
                for k in d:
                    d[k] += v[k]
            for k in sim_keys:
                e["ttl_1h_simulation"][k] += c["ttl_1h_simulation"][k]

    def tidy(e):
        e = json.loads(json.dumps(e))
        for k in ("cost_usd", "no_cache_cost_usd", "session_start_write_usd", "break_lost_usd"):
            e[k] = round(e[k], 6)
        for v in e["by_cause"].values():
            v["lost_usd"] = round(v["lost_usd"], 6)
        e["by_cause"] = dict(sorted(e["by_cause"].items(), key=lambda kv: -kv[1]["lost_usd"]))
        e["ttl_1h_simulation"] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in e["ttl_1h_simulation"].items()}
        e["no_cache_ratio"] = round(e["no_cache_cost_usd"] / e["cost_usd"], 3) if e["cost_usd"] else None
        e["cache_savings_usd"] = round(e["no_cache_cost_usd"] - e["cost_usd"], 6)
        return e
    res = tidy(out["all"])
    res["by_kind"] = {"session": tidy(out["session"]), "subagent": tidy(out["subagent"])}
    return res


# --- Actions: one fix per row, with the dollars it addresses and a rough effort -------------
# Each signal and each break cause belongs to exactly one action, so the dollars of two
# actions never overlap (they come from the cost ledger). The TTL actions use the
# simulation's net change instead of the break cost. Effort is a rough setup estimate.
ACTIONS = [
    {"id": "subagent_ttl_1h", "title": "subagent prompt cache TTL을 1시간으로",
     "how": "env CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h, 또는 --settings 파일에 \"subagentPromptCacheTtl\": \"1h\"",
     "signals": (), "causes": (), "ttl_kind": "subagent", "effort_min": 5, "per_run": True,
     "risk": "낮음: 같은 내용을 더 오래 캐시한다. 5분 안에 끝나는 subagent는 write 값이 1.6배"},
    {"id": "main_ttl_1h", "title": "메인 session prompt cache TTL을 1시간으로",
     "how": "env CLAUDE_CODE_PROMPT_CACHE_TTL=1h, 또는 --settings 파일에 \"promptCacheTtl\": \"1h\"",
     "signals": (), "causes": (), "ttl_kind": "session", "effort_min": 5, "per_run": True,
     "risk": "낮음: write 값이 1.6배"},
    {"id": "lean_prefix", "title": "쓰지 않는 MCP·skill·plugin을 빼고 시작",
     "how": "--strict-mcp-config --mcp-config <필요한 서버만>.json --disable-slash-commands (필요하면 --settings로 plugin off)",
     "signals": ("system_prompt_bloat",), "causes": (), "effort_min": 20, "per_run": True,
     "risk": "중간: 그 도구가 필요한 과제면 능력을 잃는다"},
    {"id": "targeted_prompts", "title": "프롬프트에 파일·함수·검증 명령을 짚어 주기",
     "how": "프롬프트 템플릿(파일:줄, 함수, 검증 명령) 또는 CLAUDE.md의 코드 지도",
     "signals": ("exploration_overhead", "near_duplicate_read", "redundant_tool_call", "tool_thrashing"),
     "causes": (), "effort_min": 15, "per_run": True, "risk": "낮음: 짚은 곳이 틀리면 오히려 헤맨다"},
    {"id": "output_caps", "title": "큰 도구 출력 줄이기(head, offset/limit, 파일로 저장)",
     "how": "CLAUDE.md 규칙, env BASH_MAX_OUTPUT_LENGTH", "signals": ("large_tool_result", "oversized_read"),
     "causes": (), "effort_min": 15, "per_run": True, "risk": "낮음–중간: 잘린 출력에서 정보를 놓칠 수 있다"},
    {"id": "context_hygiene", "title": "과제마다 새 session, context 100K 전에 정리",
     "how": "한 과제 = 한 session, 긴 작업은 요약 파일을 남기고 끊기",
     "signals": ("context_growth_no_compaction", "session_too_long", "session_length_warning", "post_compaction_rework"),
     "causes": ("compaction",), "effort_min": 30, "per_run": False, "risk": "중간: 요약에서 맥락이 빠진다"},
    {"id": "idle_reset", "title": "1시간 넘게 쉬기 전 /compact 하거나 새 session으로",
     "how": "작업 습관: 긴 휴식 뒤 resume 대신 요약을 들고 새 session",
     "signals": ("idle_session_cost",), "causes": ("idle_over_1h",), "effort_min": 10, "per_run": False,
     "risk": "낮음–중간: 요약에서 맥락이 빠진다"},
    {"id": "stable_prefix", "title": "session 도중 모델·도구·모드를 바꾸지 않기",
     "how": "작업 습관: 바꿀 일이 있으면 새 session", "signals": ("cache_miss_spike",),
     "causes": ("prompt_changed", "model_switch"), "effort_min": 10, "per_run": False, "risk": "낮음"},
    {"id": "subagent_discipline", "title": "subagent는 묶어서 적게 띄우기",
     "how": "프롬프트·agent 정의에 한도 명시", "signals": ("subagent_sprawl", "recursive_spawn_risk", "subagent_model_waste"),
     "causes": (), "effort_min": 20, "per_run": True, "risk": "중간: 병렬성이 줄어든다"},
    {"id": "cheaper_model", "title": "단순 작업은 더 싼 모델로",
     "how": "--model 또는 subagent model 지정", "signals": ("model_mismatch", "thinking_overhead", "ghost_trace"),
     "causes": (), "effort_min": 15, "per_run": True, "risk": "높음: 모델이 바뀌면 품질도 바뀐다"},
    {"id": "edit_discipline", "title": "실패 원인부터 고치고, 파일은 한 번에 편집",
     "how": "작업 습관", "signals": ("wasted_call_error", "edit_churn", "read_after_edit", "long_prompt"),
     "causes": (), "effort_min": 20, "per_run": False, "risk": "낮음"},
]


def actions(signals, ce, days):
    """Rank fixes by dollars per 30 days per hour of setup effort."""
    sig = {s["signal"]: s for s in signals}
    scale = 30.0 / days if days else 1.0
    out = []
    for a in ACTIONS:
        row = {k: (list(v) if isinstance(v, tuple) else v) for k, v in a.items()}
        if a.get("ttl_kind"):
            k = ce["by_kind"][a["ttl_kind"]]
            sim = k["ttl_1h_simulation"]
            if not k["cache_write_5m_tokens"]:
                continue       # this kind already writes 1-hour cache only
            usd, basis = -sim["delta_usd"], "simulation (net)"
            row["detail"] = {"breaks_avoided": sim["breaks_avoided"], "avoided_break_usd": sim["avoided_break_usd"],
                             "extra_write_usd": sim["extra_write_usd"]}
        else:
            usd = sum(sig[s]["cost_wasted_usd"] for s in a["signals"] if s in sig) + \
                sum(ce["by_cause"][c]["lost_usd"] for c in a["causes"] if c in ce["by_cause"])
            basis = "attributed cost (upper bound)"
            row["detail"] = {"signals_usd": {s: sig[s]["cost_wasted_usd"] for s in a["signals"] if s in sig},
                             "breaks_usd": {c: ce["by_cause"][c]["lost_usd"] for c in a["causes"] if c in ce["by_cause"]}}
        row["usd_window"] = round(usd, 2)
        row["usd_30d"] = round(usd * scale, 2)
        row["share_of_cost"] = round(usd / ce["cost_usd"], 4) if ce["cost_usd"] else 0.0
        row["roi_usd_per_hour"] = round(row["usd_30d"] / (a["effort_min"] / 60.0), 1)
        row["basis"] = basis
        out.append(row)
    out.sort(key=lambda r: (-r["roi_usd_per_hour"], r["id"]))
    return out


def _compact(rep):
    return {"session": rep["session"], "token_summary": rep["token_summary"], "tool_summary": rep["tool_summary"],
            "issues": rep["issues"][:15], "total_issues": rep["total_issues"],
            "issues_by_severity": rep["issues_by_severity"], "recommendations": rep["recommendations"]}


def compare_mode(root, a, b, full):
    ra = analyze_session(find_session(root, a), full=full)
    rb = analyze_session(find_session(root, b), full=full)
    ta, tb = ra["token_summary"], rb["token_summary"]
    delta = {}
    for k in ("estimated_cost_usd", "cache_hit_ratio", "max_context_tokens", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "system_tokens"):
        d = tb[k] - ta[k]
        delta[k] = round(d, 6) if isinstance(d, float) else d
    delta["turns"] = rb["session"]["turns"] - ra["session"]["turns"]
    delta["model_calls"] = rb["session"]["model_calls"] - ra["session"]["model_calls"]
    delta["total_issues"] = rb["total_issues"] - ra["total_issues"]
    delta["tool_calls"] = rb["tool_summary"]["total_calls"] - ra["tool_summary"]["total_calls"]
    sa = {i["signal"] for i in ra["issues"]}
    sb = {i["signal"] for i in rb["issues"]}
    return {"version": VERSION, "mode": "compare", "full": bool(full), "detectors_run": len(active_detectors(full)),
            "a": _compact(ra), "b": _compact(rb), "delta": delta,
            "signals": {"only_a": sorted(sa - sb), "only_b": sorted(sb - sa), "both": sorted(sa & sb)}}


# --- Dashboard (--dashboard <path>) -----------------------------------------------------------
# One self-contained Korean HTML file: inline CSS and SVG, no scripts, no network loads.
# By default it holds no session ids, file paths, prompt text or other user content: only
# totals, signal names and fixed text. --dashboard-ids adds a table of the costliest
# transcript ids.

SIGNAL_KO = {
    "context_growth_no_compaction": "정리 없이 커진 context",
    "large_tool_result": "큰 도구 결과",
    "cache_miss_spike": "cache miss 급증",
    "redundant_tool_call": "같은 도구 호출 반복",
    "subagent_sprawl": "subagent 남발",
    "ghost_trace": "일은 적은데 큰 context",
    "session_too_long": "너무 긴 session",
    "subagent_model_waste": "검색 subagent에 큰 모델",
    "recursive_spawn_risk": "subagent 연쇄 증가",
    "near_duplicate_read": "같은 범위 다시 읽기",
    "wasted_call_error": "실패한 도구 호출",
    "long_prompt": "긴 프롬프트",
    "edit_churn": "같은 파일 반복 편집",
    "read_after_edit": "편집 직후 다시 읽기",
    "oversized_read": "큰 파일 통째로 읽기",
    "tool_thrashing": "도구 사이 왔다 갔다",
    "exploration_overhead": "편집 전 탐색 과다",
    "post_compaction_rework": "compaction 뒤 다시 읽기",
    "model_mismatch": "단순 작업에 큰 모델",
    "idle_session_cost": "쉬는 동안 식은 cache",
    "system_prompt_bloat": "안 쓰는 prefix(skill·MCP 목록)",
    "thinking_overhead": "단순 단계의 긴 thinking",
    "session_length_warning": "긴 session 경고",
}
CAUSE_KO = {
    "ttl_5m_expiry": "5분 TTL 만료",
    "idle_over_1h": "1시간 넘게 쉼",
    "compaction": "compaction 뒤 다시 쓰기",
    "prompt_changed": "prefix 변경(도구·모드·메모리)",
    "model_switch": "모델 전환",
}
PART_KO = {"cache_read": "cache read", "cache_write": "cache write", "output": "output", "input": "input"}
PART_COLOR = {"cache_read": "var(--c1)", "cache_write": "var(--c2)", "output": "var(--c3)", "input": "var(--c4)"}

DASHBOARD_CSS = """
:root{--bg:#f7f7f5;--card:#ffffff;--ink:#1d1d1f;--muted:#5f6368;--line:#e3e3e0;--accent:#2457d6;
--c1:#2f6fde;--c2:#e07b2b;--c3:#7b4fd6;--c4:#8a9099;--good:#1b8a4b;--warn:#b5651d;
font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#121315;--card:#1c1d20;--ink:#ececec;--muted:#a0a4ab;--line:#30323a;
--accent:#7aa2ff;--c1:#5b8fff;--c2:#f0974a;--c3:#a383f0;--c4:#9aa0a8;--good:#4cc282;--warn:#e0a060}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);line-height:1.55}
main{max-width:1080px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0 0 12px}
.sub{color:var(--muted);font-size:14px;margin:0 0 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.kpi .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}.kpi .l{color:var(--muted);font-size:13px}
.kpi .n{color:var(--muted);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:7px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12.5px}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tw{overflow-x:auto}.tag{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:999px;
border:1px solid var(--line);color:var(--muted);margin-left:4px;white-space:nowrap}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.big{font-size:17px}.note{color:var(--muted);font-size:12.5px}.legend span{margin-right:14px;font-size:13px}
.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.top3{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.top3 .card{margin:0;border-top:3px solid var(--accent)}.top3 .rank{color:var(--accent);font-weight:700}
.top3 .how{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:var(--bg);
padding:6px 8px;border-radius:6px;margin-top:8px;word-break:break-word}
svg text{fill:var(--ink);font-size:12px}svg .m{fill:var(--muted)}
"""


def _usd(v, digits=2):
    v = v or 0.0
    return ("-$" if v < 0 else "$") + ("{:,.%df}" % digits).format(abs(v))


def _num(v):
    return "{:,}".format(int(v or 0))


def _pct(v, digits=1):
    return ("%." + str(digits) + "f%%") % (100.0 * (v or 0.0))


def _esc(s):
    return html.escape(str(s), quote=True)


def _hbars(rows, width=640, label_w=220, value_fmt=_usd):
    """rows: [(label, value, note)] -> inline SVG horizontal bars."""
    if not rows:
        return '<p class="note">해당 없음</p>'
    top = max(v for _l, v, _n in rows) or 1.0
    h = 26
    bar_w = width - label_w - 150
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="막대 그래프">' % (width, h * len(rows) + 4)]
    for i, (label, v, note) in enumerate(rows):
        y = i * h + 4
        w = max(1.0, bar_w * (v / top)) if v > 0 else 0
        out.append('<text x="0" y="%d">%s</text>' % (y + 14, _esc(label)))
        out.append('<rect x="%d" y="%d" width="%.1f" height="16" rx="3" fill="var(--c1)"/>' % (label_w, y + 2, w))
        out.append('<text x="%.1f" y="%d">%s <tspan class="m">%s</tspan></text>' % (
            label_w + w + 6, y + 14, _esc(value_fmt(v)), _esc(note)))
    out.append("</svg>")
    return "".join(out)


def _stacked(parts, total, width=640):
    """parts: [(key, usd)] -> one stacked bar with a legend."""
    x = 0.0
    seg = []
    for k, v in parts:
        w = width * (v / total) if total else 0
        if w > 0:
            seg.append('<rect x="%.1f" y="0" width="%.1f" height="26" fill="%s"/>' % (x, w, PART_COLOR[k]))
        x += w
    legend = "".join('<span><i class="dot" style="background:%s"></i>%s %s · %s</span>' % (
        PART_COLOR[k], PART_KO[k], _pct(v / total if total else 0, 0), _usd(v, 0)) for k, v in parts)
    return ('<svg viewBox="0 0 %d 26" width="100%%" role="img" aria-label="비용 구성">%s</svg>'
            '<div class="legend" style="margin-top:8px">%s</div>') % (width, "".join(seg), legend)


def _dashboard_view(result):
    """Normalize an aggregate or session result for the dashboard."""
    mode = result.get("mode")
    if mode == "aggregate":
        days = parse_window(result["window"])
        return {"mode": mode, "scope": "최근 %s" % result["window"], "days": days,
                "project": result.get("project") or "전체 프로젝트", "counts": result["transcripts"],
                "model_calls": result["model_calls"], "tokens": result["token_summary"],
                "ce": result["cache_economics"], "signals": result["signals"], "actions": result["actions"],
                "top_actions": result["top_actions"], "waste_usd": result["total_cost_wasted_usd"],
                "transcripts": result.get("top_transcripts") or []}
    if mode == "session":
        c = result["cache_economics"]
        kind = result["session"]["kind"]
        sim = c["ttl_1h_simulation"]
        zero = {"cache_write_5m_tokens": 0, "ttl_1h_simulation": {k: 0 for k in sim}}
        kinds = {"session": dict(zero), "subagent": dict(zero)}
        kinds[kind] = {"cache_write_5m_tokens": c["cache_write_5m_tokens"], "ttl_1h_simulation": sim,
                       "cost_usd": c["actual_cost_usd"], "cache_write_1h_tokens": c["cache_write_1h_tokens"],
                       "breaks": c["breaks"]["count"], "break_lost_usd": c["breaks"]["lost_usd"], "transcripts": 1}
        ce = {"cost_usd": c["actual_cost_usd"], "no_cache_cost_usd": c["no_cache_cost_usd"],
              "no_cache_ratio": c["no_cache_ratio"], "cache_savings_usd": c["cache_savings_usd"],
              "breaks": c["breaks"]["count"], "break_lost_usd": c["breaks"]["lost_usd"],
              "by_cause": c["breaks"]["by_cause"], "by_kind": kinds,
              "ttl_1h_simulation": sim, "session_start_write_usd": c["session_start_write_usd"]}
        sig = {}
        for i in result["issues"]:
            e = sig.setdefault(i["signal"], {"signal": i["signal"], "severity": i["severity"], "issues": 0,
                                             "tokens_wasted": 0, "cost_wasted_usd": 0.0, "frequency": ""})
            e["issues"] += 1
            e["tokens_wasted"] += i["tokens_wasted"]
            e["cost_wasted_usd"] += i["cost_wasted_usd"]
        for e in sig.values():
            e["frequency"] = "issue %d개" % e["issues"]
        signals = list(sig.values())
        acts = actions(signals, ce, None)
        n_sub = 1 if kind == "subagent" else 0
        return {"mode": mode, "scope": "session 하나", "days": None, "project": result["session"]["project"],
                "counts": {"total": 1, "sessions": 1 - n_sub, "subagents": n_sub},
                "model_calls": result["session"]["model_calls"], "tokens": result["token_summary"], "ce": ce,
                "signals": signals, "actions": acts, "top_actions": [a["id"] for a in acts if a["usd_30d"] > 0][:3],
                "waste_usd": result["total_cost_wasted_usd"], "transcripts": [brief(result)]}
    raise AnalyzerError("--dashboard needs --aggregate or one session")


def render_dashboard(result, include_ids=False):
    v = _dashboard_view(result)
    t, ce = v["tokens"], v["ce"]
    cost = t["estimated_cost_usd"]
    per = "30일" if v["days"] else "이 session"
    action_of = {s: a for a in ACTIONS for s in a["signals"]}
    h = []
    h.append('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>토큰·캐시 진단</title><style>%s</style></head><body><main>' % DASHBOARD_CSS)
    h.append('<h1>Claude Code 토큰·캐시 진단</h1>')
    h.append('<p class="sub">%s · %s · session %d개 + subagent %d개 · 모델 호출 %s회 · 분석기 v%s · 만든 날 %s</p>' % (
        _esc(v["project"]), _esc(v["scope"]), v["counts"]["sessions"], v["counts"]["subagents"],
        _num(v["model_calls"]), VERSION, datetime.now().strftime("%Y-%m-%d")))

    # KPI tiles
    sim_sub = ce["by_kind"]["subagent"]["ttl_1h_simulation"]
    h.append('<div class="grid">')
    for label, value, note in (
            ("총비용 (정가 기준 추정)", _usd(cost), "[기록] transcript 토큰 × 정가"),
            ("cache hit", _pct(t["cache_hit_ratio"]), "cache read ÷ 전체 입력 토큰"),
            ("cache가 없었다면", "%s (%.1f배)" % (_usd(ce["no_cache_cost_usd"], 0), ce["no_cache_ratio"] or 0),
             "[기록] 같은 호출을 cache 없이 보낸 가정"),
            ("session 중간 cache break", "%s회 · +%s" % (_num(ce["breaks"]), _usd(ce["break_lost_usd"])),
             "[기록] 다시 쓰느라 더 낸 돈 (비용의 %s)" % _pct(ce["break_lost_usd"] / cost if cost else 0))):
        h.append('<div class="card kpi"><div class="l">%s</div><div class="v">%s</div><div class="n">%s</div></div>' % (
            _esc(label), _esc(value), _esc(note)))
    h.append('</div>')

    # cost split
    split = t["cost_split_usd"]
    parts = [(k, split[k]) for k in ("cache_read", "cache_write", "output", "input")]
    h.append('<section class="card"><h2>비용은 어디로 갔나 <span class="tag">[기록]</span></h2>%s' % _stacked(parts, cost))
    h.append('<div class="tw" style="margin-top:14px"><table><tr><th>토큰 종류</th><th class="r">토큰</th>'
             '<th class="r">비용</th><th class="r">비중</th></tr>')
    rows = [("input (cache 안 씀)", t["input_tokens"], split["input"]),
            ("cache write 5분", t["cache_write_5m_tokens"], None),
            ("cache write 1시간", t["cache_write_1h_tokens"], None),
            ("cache write 합계", t["cache_write_tokens"], split["cache_write"]),
            ("cache read", t["cache_read_tokens"], split["cache_read"]),
            ("output", t["output_tokens"], split["output"])]
    for label, n, usd in rows:
        h.append('<tr><td>%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td></tr>' % (
            _esc(label), _num(n), _usd(usd) if usd is not None else "", _pct(usd / cost) if usd is not None and cost else ""))
    h.append('</table></div>')
    kinds = ce["by_kind"]
    if v["mode"] == "aggregate":
        h.append('<div class="tw" style="margin-top:14px"><table><tr><th>누가 썼나</th><th class="r">transcript</th>'
                 '<th class="r">비용</th><th class="r">비중</th><th class="r">write 5분</th><th class="r">write 1시간</th>'
                 '<th class="r">break</th></tr>')
        for k, label in (("session", "메인 session"), ("subagent", "subagent")):
            e = kinds[k]
            h.append('<tr><td>%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td>'
                     '<td class="r">%s</td><td class="r">%s회 · %s</td></tr>' % (
                         label, _num(e["transcripts"]), _usd(e["cost_usd"]), _pct(e["cost_usd"] / cost if cost else 0),
                         _num(e["cache_write_5m_tokens"]), _num(e["cache_write_1h_tokens"]), _num(e["breaks"]),
                         _usd(e["break_lost_usd"])))
        h.append('</table></div>')
    by_model = t.get("cost_by_model_usd") or {}
    if by_model:
        h.append('<p class="note" style="margin-top:10px">모델별: %s</p>' % _esc(" · ".join(
            "%s %s" % (k, _pct(val / cost if cost else 0, 0)) for k, val in by_model.items())))
    h.append('</section>')

    # no-cache line
    h.append('<section class="card"><h2>cache가 없었다면 <span class="tag">[기록]</span></h2><p class="big">'
             '같은 호출을 cache 없이 보냈다면 <b>%s</b>였습니다. 실제는 <b>%s</b>, cache가 <b>%s</b>(%s)를 아꼈습니다.</p>'
             '<p class="note">계산: 모든 입력 토큰(input + cache write + cache read) × 기본 input 단가 + output × output 단가. '
             '실제: cache write는 TTL별 단가, cache read는 read 단가.</p></section>' % (
                 _usd(ce["no_cache_cost_usd"]), _usd(cost), _usd(ce["cache_savings_usd"]),
                 _pct(ce["cache_savings_usd"] / ce["no_cache_cost_usd"] if ce["no_cache_cost_usd"] else 0, 0)))

    # breaks by cause + TTL sim
    causes = sorted(ce["by_cause"].items(), key=lambda kv: -kv[1]["lost_usd"])
    h.append('<section class="card"><h2>cache가 깨진 곳 <span class="tag">[기록]</span></h2>')
    h.append(_hbars([(CAUSE_KO.get(k, k), e["lost_usd"], "%d회" % e["count"]) for k, e in causes]))
    h.append('<p class="note">break 기준: 앞 호출 context의 절반도 cache에서 못 읽고 %s 토큰 넘게 다시 쓴 호출. '
             '손해 = 다시 쓴 토큰 × (write 단가 − read 단가). 원인: 모델이 바뀌면 "모델 전환", 1시간 넘게 멈췄으면 '
             '"1시간 넘게 쉼", 5분 TTL로 쓰고 5분 넘게 멈췄으면 "5분 TTL 만료", compaction 직후면 "compaction", 나머지는 "prefix 변경".</p>' % _num(BREAK_MIN_WRITE))
    if kinds["subagent"]["cache_write_5m_tokens"]:
        h.append('<p class="big" style="margin-top:12px">subagent를 1시간 TTL로 돌렸다면 <span class="tag">[추정·시뮬레이션]</span><br>'
                 'break %s회를 피해 <b>%s</b>를 덜 내고, 대신 1시간 write가 비싸서 <b>%s</b>를 더 냅니다. 순변화 <b>%s</b>.</p>' % (
                     _num(sim_sub["breaks_avoided"]), _usd(sim_sub["avoided_break_usd"]), _usd(sim_sub["extra_write_usd"]),
                     _usd(sim_sub["delta_usd"])))
    h.append('</section>')

    # waste patterns ranked by $
    sigs = sorted(v["signals"], key=lambda s: (-s["cost_wasted_usd"], s["signal"]))
    h.append('<section class="card"><h2>낭비 패턴, 돈 순서 <span class="tag">[추정·상한]</span></h2><div class="tw"><table>'
             '<tr><th>패턴</th><th>빈도</th><th class="r">비용(겹치지 않게)</th><th class="r">총비용 대비</th>'
             '<th>고치는 법</th><th class="r">노력</th><th class="r">ROI ($/시간)</th></tr>')
    act_by_id = {a["id"]: a for a in v["actions"]}
    for s in sigs:
        a = action_of.get(s["signal"])
        ar = act_by_id.get(a["id"]) if a else None
        h.append('<tr><td>%s<br><span class="code">%s</span></td><td>%s</td><td class="r">%s</td><td class="r">%s</td>'
                 '<td>%s</td><td class="r">%s</td><td class="r">%s</td></tr>' % (
                     _esc(SIGNAL_KO.get(s["signal"], s["signal"])), _esc(s["signal"]), _esc(s["frequency"]),
                     _usd(s["cost_wasted_usd"]), _pct(s["cost_wasted_usd"] / cost if cost else 0),
                     _esc(a["title"]) if a else "", "%d분" % a["effort_min"] if a else "",
                     _num(ar["roi_usd_per_hour"]) if ar else ""))
    h.append('</table></div><p class="note">비용은 호출마다 실제로 낸 돈(input·write·read·output)에서 한 번만 떼어 냅니다. '
             'cache break를 먼저, 그다음 탐지기 순서로 뗍니다. 그래서 패턴끼리 겹치지 않고 합(%s, 총비용의 %s)이 총비용을 넘지 않습니다. '
             '"너무 긴 session"은 비용을 context 증가와 cache break에 넘기므로 $0입니다. 이미지 결과는 장당 %s 토큰으로 계산합니다.</p></section>' % (
                 _usd(v["waste_usd"]), _pct(v["waste_usd"] / cost if cost else 0), _num(BINARY_RESULT_TOKENS)))

    # actions ranked by ROI
    h.append('<section class="card"><h2>고칠 것, ROI 순서 <span class="tag">[추정]</span></h2><div class="tw"><table>'
             '<tr><th>고칠 것</th><th class="r">아낄 돈 / %s</th><th>근거</th><th class="r">노력</th><th>품질 위험</th>'
             '<th>run마다 적용</th><th class="r">ROI ($/시간)</th></tr>' % per)
    for a in v["actions"]:
        basis = "시뮬레이션 순변화" if a["basis"].startswith("simulation") else "배분한 비용(상한)"
        h.append('<tr><td>%s</td><td class="r">%s</td><td>%s</td><td class="r">%d분</td><td>%s</td><td>%s</td>'
                 '<td class="r">%s</td></tr>' % (
                     _esc(a["title"]), _usd(a["usd_30d"]), basis, a["effort_min"], _esc(a["risk"]),
                     "예" if a["per_run"] else "아니요(습관)", _num(a["roi_usd_per_hour"])))
    h.append('</table></div><p class="note">아낄 돈은 그 패턴이 쓴 돈의 상한입니다(TTL만 시뮬레이션 순변화). '
             '노력은 처음 한 번 설정하는 데 드는 대략의 분입니다. ROI = 아낄 돈 ÷ 노력(시간).</p></section>')

    # top 3
    top = [act_by_id[i] for i in v["top_actions"] if i in act_by_id]
    h.append('<section><h2>추천 3가지</h2><div class="top3">')
    for n, a in enumerate(top, 1):
        h.append('<div class="card"><div class="rank">%d위 · %s / %s</div><div class="big"><b>%s</b></div>'
                 '<div class="note">노력 %d분 · 위험: %s</div><div class="how">%s</div></div>' % (
                     n, _usd(a["usd_30d"]), per, _esc(a["title"]), a["effort_min"], _esc(a["risk"]), _esc(a["how"])))
    h.append('</div></section>')

    if include_ids and v["transcripts"]:
        h.append('<section class="card" style="margin-top:16px"><h2>비용 상위 transcript</h2><div class="tw"><table>'
                 '<tr><th>id</th><th>종류</th><th class="r">turn</th><th class="r">비용</th><th class="r">cache hit</th>'
                 '<th>top signal</th></tr>')
        for b in v["transcripts"]:
            h.append('<tr><td class="code">%s</td><td>%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td>'
                     '<td>%s</td></tr>' % (_esc(b["id"]), _esc(b["kind"]), _num(b["turns"]), _usd(b["estimated_cost_usd"]),
                                           _pct(b["cache_hit_ratio"]), _esc(b["top_signal"] or "")))
        h.append('</table></div></section>')

    h.append('<section class="card" style="margin-top:16px"><h2>계산 방법</h2><ul class="note">'
             '<li>[기록] = transcript에서 직접 센 값, [추정] = 계산이나 가정. 비용은 정가 기준 추정이며 구독 청구액이 아닙니다.</li>'
             '<li>단가: %s.</li><li>사용량은 message id마다 한 번만 셉니다(스트리밍 조각 중복 제거). '
             'subagent transcript는 따로 세고 합칩니다.</li>'
             '<li>이 페이지에는 session id, 파일 경로, 프롬프트 내용이 들어 있지 않습니다%s.</li></ul></section>' % (
                 _esc(COST_METHOD), "" if not include_ids else " (이 사본은 --dashboard-ids로 transcript id를 넣었습니다)"))
    h.append('</main></body></html>\n')
    return "".join(h)


def write_dashboard(path, result, include_ids=False):
    text = render_dashboard(result, include_ids)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# --- History ----------------------------------------------------------------------------------

def history_path():
    return os.environ.get("TOKEN_EFFICIENCY_HISTORY") or os.path.expanduser(
        "~/.claude/token-efficiency/history.jsonl")


def save_history(result):
    mode = result.get("mode")
    if mode == "session":
        ts, target = result["token_summary"], result["session"]["id"]
        issues = result["issues"]
        signals = {}
        for i in issues:
            signals[i["signal"]] = signals.get(i["signal"], 0) + 1
        waste = sum(i["tokens_wasted"] for i in issues)
        n_issues = result["total_issues"]
    elif mode == "aggregate":
        ts = result["token_summary"]
        target = "%s:%s" % (result["window"], result["project"] or "*")
        signals = {s["signal"]: s["transcripts_affected"] for s in result["signals"]}
        waste = result["total_tokens_wasted"]
        n_issues = result["total_issues"]
    else:
        return
    entry = {"saved_at": iso(time.time()), "mode": mode, "target": target,
             "estimated_cost_usd": ts["estimated_cost_usd"], "cache_hit_ratio": ts["cache_hit_ratio"],
             "max_context_tokens": ts["max_context_tokens"], "total_issues": n_issues,
             "tokens_wasted": waste, "signals": signals}
    path = history_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def history_mode():
    path = history_path()
    entries = []
    if os.path.exists(path):
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    entries.append(json.loads(raw))
                except ValueError:
                    continue
    trend = None
    if len(entries) >= 2:
        a, b = entries[0], entries[-1]
        trend = {k: round(b[k] - a[k], 6) for k in ("estimated_cost_usd", "cache_hit_ratio",
                                                     "max_context_tokens", "total_issues", "tokens_wasted")}
        trend["from"], trend["to"] = a["saved_at"], b["saved_at"]
    return {"version": VERSION, "mode": "history", "path": path, "entries": entries, "trend": trend}


# --- CLI ----------------------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Read-only token analyzer for Claude Code transcripts.")
    p.add_argument("--current", action="store_true", help="the current session (default)")
    p.add_argument("--session", help="one session by id (or a path to a .jsonl file)")
    p.add_argument("--last", help="sessions in a window: 7d, 2w, 1m")
    p.add_argument("--aggregate", action="store_true", help="patterns across sessions")
    p.add_argument("--project", help="filter by project name")
    p.add_argument("--turn", type=int, help="deep dive into one turn")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="two sessions side by side")
    p.add_argument("--detail", action="store_true", help="per-turn token breakdown")
    p.add_argument("--full", action="store_true", help="also run the 2 INFO detectors")
    p.add_argument("--history", action="store_true", help="improvement trend from saved runs")
    p.add_argument("--save-history", action="store_true", help="append this run to the history file")
    p.add_argument("--format", default="json", choices=["json"])
    p.add_argument("--dashboard", metavar="PATH", help="also write a self-contained Korean HTML dashboard "
                   "(with --aggregate or one session); no session ids, paths or prompt text")
    p.add_argument("--dashboard-ids", action="store_true", help="put the costliest transcript ids in the dashboard")
    p.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"))
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.projects_dir
    try:
        if args.history:
            result = history_mode()
        elif args.compare:
            result = compare_mode(root, args.compare[0], args.compare[1], args.full)
        elif args.last or args.aggregate or (args.project and not args.session):
            window = args.last or "30d"
            files = discover_window(root, window, args.project)
            if args.aggregate:
                result = aggregate_mode(files, window, args.project, args.full)
            else:
                result = list_mode(files, window, args.project, args.full)
        else:
            path = find_session(root, args.session) if args.session else find_current(root)
            result = analyze_session(path, full=args.full, detail=args.detail, turn=args.turn)
        if args.save_history:
            save_history(result)
        if args.dashboard:
            write_dashboard(args.dashboard, result, include_ids=args.dashboard_ids)
            result["dashboard"] = os.path.abspath(args.dashboard)
    except AnalyzerError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
