"""Acceptance tests for analyze-tokens.py (spec A.10). Standard library only.

Run: python3 -B -m unittest discover -s scripts/tests -v   (-B: no __pycache__ in the skill)
"""
import glob
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True   # keep the skill folder clean
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "analyze-tokens.py")

_spec = importlib.util.spec_from_file_location("analyze_tokens", SCRIPT)
at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(at)

BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
BASH = ("Bash", {"command": "echo hi"})


class Tx:
    """Builds a synthetic Claude Code transcript.

    Every user record starts a new turn. step() = one model call in the
    current turn, then one user record with its tool results (next turn).
    """

    def __init__(self, sid="sess-0001", model="claude-opus-5-5"):
        self.sid = sid
        self.model = model
        self.records = []
        self.clock = 0.0
        self.n_msg = 0
        self.n_tool = 0

    def _ts(self, gap):
        self.clock += gap
        return (BASE + timedelta(seconds=self.clock)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def user(self, content="do the task", gap=1.0, **extra):
        rec = {"type": "user", "message": {"role": "user", "content": content},
               "timestamp": self._ts(gap), "sessionId": self.sid, "cwd": "/tmp/demo-proj",
               "uuid": "u%d" % len(self.records)}
        rec.update(extra)
        self.records.append(rec)
        return self

    def call(self, ctx=10000, hit=0.9, inp=None, cr=None, cw=None, out=50, text="",
             thinking=None, tools=(), model=None, gap=1.0, chunks=False, msg_id=None, cw1h=None):
        if cr is None:
            inp = 2 if inp is None else inp
            cr = int((ctx - inp) * hit)
            cw = ctx - inp - cr
        if inp is None:
            inp = 0
        blocks = []
        if thinking is not None:
            blocks.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
        if text:
            blocks.append({"type": "text", "text": text})
        ids = []
        for name, tinput in tools:
            self.n_tool += 1
            tid = "toolu_%04d" % self.n_tool
            ids.append(tid)
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": tinput})
        self.n_msg += 1
        mid = msg_id or "msg_%04d" % self.n_msg
        usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                 "cache_creation_input_tokens": cw}
        if cw1h is not None:
            usage["cache_creation"] = {"ephemeral_1h_input_tokens": cw1h, "ephemeral_5m_input_tokens": cw - cw1h}
        ts = self._ts(gap)
        parts = [[b] for b in blocks] if chunks else [blocks]
        if not parts:
            parts = [[]]
        for i, part in enumerate(parts):
            u = dict(usage)
            if i < len(parts) - 1:
                u["output_tokens"] = 8  # streaming chunks carry a partial count
            self.records.append({"type": "assistant", "timestamp": ts, "sessionId": self.sid,
                                 "uuid": "a%d" % len(self.records),
                                 "message": {"id": mid, "role": "assistant",
                                             "model": model or self.model,
                                             "content": part, "usage": u}})
        return ids

    def attach(self, kind, text, gap=0.0):
        """An attachment record (skill listing, MCP listing, CLAUDE.md ...)."""
        self.records.append({"type": "attachment", "attachment": {"type": kind}, "rendered": text,
                             "timestamp": self._ts(gap), "sessionId": self.sid, "uuid": "t%d" % len(self.records)})
        return self

    def results(self, ids, contents, gap=1.0, split=False, **extra):
        blocks = []
        for tid, c in zip(ids, contents):
            if isinstance(c, dict):
                blocks.append({"type": "tool_result", "tool_use_id": tid,
                               "content": c.get("content", "ok"), "is_error": c.get("is_error", False)})
            else:
                blocks.append({"type": "tool_result", "tool_use_id": tid, "content": c})
        if split:
            for b in blocks:
                self.user([b], gap=gap, **extra)
        else:
            self.user(blocks, gap=gap, **extra)

    def step(self, tools, results=None, gap=1.0, **kw):
        ids = self.call(tools=tools, gap=gap, **kw)
        self.results(ids, results or ["ok"] * len(ids))
        return ids

    def write(self, root, project="-tmp-demo-proj", name=None, subagent_of=None):
        if subagent_of:
            d = os.path.join(root, project, subagent_of, "subagents")
        else:
            d = os.path.join(root, project)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, (name or self.sid) + ".jsonl")
        with open(path, "w") as fh:
            for r in self.records:
                fh.write(json.dumps(r) + "\n")
        return path


def lines(n, start=1):
    return "".join("%6d\tline %d\n" % (i, i) for i in range(start, start + n))


# --- detector fixtures: (trigger, just under the threshold) -------------------

def f_context_growth(max_ctx, steps=20):
    t = Tx().user()
    for i in range(steps):
        t.step([("Bash", {"command": "echo %d" % i})], ctx=10000 + (max_ctx - 10000) * i // (steps - 1), hit=0.95)
    return t


def f_large_result(chars):
    t = Tx().user()
    t.step([("Bash", {"command": "cat big.log"})], results=["x" * chars])
    return t


def f_cache_miss(cr, cw):
    t = Tx().user()
    t.step([BASH], ctx=20000, hit=0.0)          # turn 1 is excluded
    t.step([("Bash", {"command": "ls"})], inp=2, cr=cr, cw=cw)
    return t


def f_cache_miss_ctx(ctx):
    t = Tx().user()
    t.step([BASH], ctx=20000, hit=0.0)
    t.step([("Bash", {"command": "ls"})], ctx=ctx, hit=0.2)
    return t


def f_redundant(second_path):
    t = Tx().user()
    t.step([("Read", {"file_path": "/repo/a.py"})], results=[lines(10)])
    t.step([("Read", {"file_path": second_path})], results=[lines(10)])
    return t


AGENT_WORK = ("Agent", {"description": "write release notes", "prompt": "Write notes.",
                        "subagent_type": "general-purpose"})


def f_sprawl(gap_turns, n=5):
    t = Tx().user()
    for k in range(n):
        t.step([AGENT_WORK], results=["done %d" % k])
        for j in range(gap_turns - 1):
            t.step([("Bash", {"command": "echo %d-%d" % (k, j)})])
    return t


def f_ghost(ctx, n_tools):
    t = Tx().user("ping")
    for i in range(n_tools):
        t.step([("Bash", {"command": "date %d" % i})], ctx=ctx)
    t.call(ctx=ctx, text="pong")
    return t


def f_session_too_long(steps, total_cw=150000):
    t = Tx().user()
    for i in range(steps):
        cw = total_cw - (steps - 1) * 1000 if i == 0 else 1000
        t.step([("Bash", {"command": "echo %d" % i})], inp=2, cr=9000, cw=cw)
    return t


def f_subagent_model(models):
    t = Tx().user()
    tools = []
    for i, m in enumerate(models):
        inp = {"description": "Search the codebase for config %d" % i, "prompt": "Find it.",
               "subagent_type": "Explore"}
        if m:
            inp["model"] = m
        tools.append(("Agent", inp))
    t.step(tools, results=["found"] * len(tools))
    return t


def f_spawn(counts):
    t = Tx().user()
    for k, n in enumerate(counts):
        tools = [("Agent", {"description": "write part %d-%d" % (k, i), "prompt": "go",
                            "subagent_type": "general-purpose", "model": "sonnet"}) for i in range(n)]
        t.step(tools, results=["done"] * n)
    return t


def f_near_dup(second_offset):
    t = Tx().user()
    t.step([("Read", {"file_path": "/repo/a.py", "offset": 1, "limit": 100})], results=[lines(100)])
    t.step([("Read", {"file_path": "/repo/a.py", "offset": second_offset, "limit": 100})],
           results=[lines(100, second_offset)])
    return t


def f_error(is_error):
    t = Tx().user()
    t.step([("Bash", {"command": "make"})], results=[{"content": "boom", "is_error": is_error}])
    return t


def f_long_prompt_tx(text):
    t = Tx().user(text)
    t.call(text="ok")
    return t


def f_edit_churn(n):
    t = Tx().user()
    for i in range(n):
        t.step([("Edit", {"file_path": "/repo/a.py", "old_string": "v%d" % i, "new_string": "v%d" % (i + 1)})])
    return t


def f_read_after_edit(between):
    t = Tx().user()
    t.step([("Edit", {"file_path": "/repo/a.py", "old_string": "a", "new_string": "b"})])
    for i in range(between):
        t.step([("Bash", {"command": "echo %d" % i})])
    t.step([("Read", {"file_path": "/repo/a.py"})], results=[lines(5)])
    return t


def f_oversized(chars, limit=None):
    t = Tx().user()
    inp = {"file_path": "/repo/big.txt"}
    if limit:
        inp["limit"] = limit
    t.step([("Read", inp)], results=["y" * chars])
    return t


def f_thrash(seq):
    t = Tx().user()
    for i, name in enumerate(seq):
        inp = {"command": "echo %d" % i} if name == "Bash" else {"file_path": "/repo/f%d.py" % i}
        t.step([(name, inp)])
    return t


def f_exploration(n_reads):
    t = Tx().user()
    for i in range(n_reads):
        t.step([("Read", {"file_path": "/repo/m%d.py" % i})], results=[lines(3)])
    t.step([("Edit", {"file_path": "/repo/m0.py", "old_string": "a", "new_string": "b"})])
    return t


def f_rework(reread):
    t = Tx().user()
    t.step([("Read", {"file_path": "/repo/a.py"})], ctx=80000, results=[lines(5)])
    t.step([("Read", {"file_path": "/repo/b.py"})], ctx=90000, results=[lines(5)])
    t.step([("Bash", {"command": "echo compacted"})], ctx=30000, hit=0.0)   # > 50% drop
    for p in reread:
        t.step([("Read", {"file_path": p})], ctx=35000, results=[lines(5)])
    t.call(ctx=36000, text="done")
    return t


def f_mismatch(n, model="claude-opus-5-5"):
    t = Tx(model=model).user()
    for i in range(n):
        t.step([("Bash", {"command": "ls %d" % i})])
    t.call(text="Here is the full answer. " * 20)
    return t


def f_idle(gaps, ctx=40000):
    t = Tx().user()
    t.call(ctx=ctx, text="answer one")
    for g in gaps:
        t.user("next question", gap=g)
        t.call(ctx=ctx, text="answer")
    return t


def f_system(ctx):
    t = Tx().user("h" * 400)
    t.call(ctx=ctx, text="hello")
    return t


def f_bloat(optional_chars, ctx=40000):
    """A session whose skill listing is optional_chars long (tokens = chars / 4)."""
    t = Tx().user("h" * 400)
    t.attach("instructions", "c" * 30000)              # CLAUDE.md: never counted
    t.attach("skill_listing", "s" * optional_chars)
    t.call(ctx=ctx, text="hello")
    return t


def f_thinking(chars):
    t = Tx().user()
    t.step([("Bash", {"command": "ls"})], thinking="t" * chars)
    t.call(text="done")
    return t


def f_length(steps, peak=120001):
    t = Tx().user()
    for i in range(steps):
        t.step([("Bash", {"command": "echo %d" % i})], ctx=peak if i == steps - 1 else 60000)
    return t


def _turns(t):
    return sum(1 for r in t.records if r["type"] == "user")


def _filler_until(t, turn, ctx=35000):
    while _turns(t) < turn:
        t.call(ctx=ctx, text="working")
        t.user("go on")


GROWTH_PREV = 10000 + 90001 * 9 // 19     # context of the call before the drop


def f_growth_with_drop(low):
    t = Tx().user()
    for i in range(20):
        ctx = 10000 + 90001 * i // 19
        if i == 10:
            ctx = low
        t.step([("Bash", {"command": "echo %d" % i})], ctx=ctx, hit=0.95)
    return t


def f_thrash_spread(last_turn):
    t = Tx().user()
    for i, name in enumerate(["Read", "Bash", "Read", "Bash"]):
        t.step([(name, {"command": "echo %d" % i} if name == "Bash" else {"file_path": "/repo/f%d.py" % i})])
    _filler_until(t, last_turn)
    t.step([("Read", {"file_path": "/repo/last.py"})])
    return t


def f_rework_drop(low):
    t = Tx().user()
    t.step([("Read", {"file_path": "/repo/a.py"})], ctx=80000, results=[lines(5)])
    t.step([("Read", {"file_path": "/repo/b.py"})], ctx=90000, results=[lines(5)])
    t.step([("Bash", {"command": "echo compacted"})], ctx=low, hit=0.0)
    for p in ("/repo/a.py", "/repo/b.py"):
        t.step([("Read", {"file_path": p})], ctx=low + 1000, results=[lines(5)])
    t.call(ctx=low + 2000, text="done")
    return t


def f_rework_window(second_turn):
    t = Tx().user()
    t.step([("Read", {"file_path": "/repo/a.py"})], ctx=80000, results=[lines(5)])
    t.step([("Read", {"file_path": "/repo/b.py"})], ctx=90000, results=[lines(5)])
    t.step([("Bash", {"command": "echo compacted"})], ctx=30000, hit=0.0)    # drop at turn 3
    t.step([("Read", {"file_path": "/repo/a.py"})], ctx=35000, results=[lines(5)])
    _filler_until(t, second_turn)
    t.step([("Read", {"file_path": "/repo/b.py"})], ctx=35000, results=[lines(5)])
    t.call(ctx=36000, text="done")
    return t


# signal -> (trigger fixture, just-under fixture, needs --full)
CASES = {
    "context_growth_no_compaction": (lambda: f_context_growth(100001), lambda: f_context_growth(100000), False),
    "large_tool_result": (lambda: f_large_result(50001), lambda: f_large_result(50000), False),
    "cache_miss_spike": (lambda: f_cache_miss(19999, 19999), lambda: f_cache_miss(20000, 19998), False),
    "redundant_tool_call": (lambda: f_redundant("/repo/a.py"), lambda: f_redundant("/repo/b.py"), False),
    "subagent_sprawl": (lambda: f_sprawl(2), lambda: f_sprawl(3), False),
    "ghost_trace": (lambda: f_ghost(60000, 2), lambda: f_ghost(60000, 3), False),
    "session_too_long": (lambda: f_session_too_long(30), lambda: f_session_too_long(29), False),
    "subagent_model_waste": (lambda: f_subagent_model([None, None, "opus"]),
                             lambda: f_subagent_model([None, None, "haiku"]), False),
    "recursive_spawn_risk": (lambda: f_spawn([1, 2, 3]), lambda: f_spawn([1, 2, 2]), False),
    "near_duplicate_read": (lambda: f_near_dup(50), lambda: f_near_dup(51), False),
    "wasted_call_error": (lambda: f_error(True), lambda: f_error(False), False),
    "long_prompt": (lambda: f_long_prompt_tx("a" * 5001),
                    lambda: f_long_prompt_tx("a" * 5000 + "<system-reminder>" + "b" * 2000 + "</system-reminder>"),
                    False),
    "edit_churn": (lambda: f_edit_churn(3), lambda: f_edit_churn(2), False),
    "read_after_edit": (lambda: f_read_after_edit(1), lambda: f_read_after_edit(2), False),
    "oversized_read": (lambda: f_oversized(20001), lambda: f_oversized(20000), False),
    "tool_thrashing": (lambda: f_thrash(["Read", "Bash", "Read", "Bash", "Read"]),
                       lambda: f_thrash(["Read", "Bash", "Read", "Bash"]), False),
    "exploration_overhead": (lambda: f_exploration(15), lambda: f_exploration(14), False),
    "post_compaction_rework": (lambda: f_rework(["/repo/a.py", "/repo/b.py"]), lambda: f_rework(["/repo/a.py"]), False),
    "model_mismatch": (lambda: f_mismatch(5), lambda: f_mismatch(4), False),
    "idle_session_cost": (lambda: f_idle([601, 601, 601]), lambda: f_idle([601, 601, 600]), False),
    "system_prompt_bloat": (lambda: f_bloat(20004), lambda: f_bloat(20000), False),   # 5,001 vs 5,000 tokens
    "thinking_overhead": (lambda: f_thinking(2001), lambda: f_thinking(2000), True),
    "session_length_warning": (lambda: f_length(50), lambda: f_length(49), True),
}

# Second conditions of two-part rules: (signal, trigger, just under, needs --full)
EXTRA_CASES = [
    ("context_growth_no_compaction", lambda: f_context_growth(100001, steps=19),
     lambda: f_context_growth(100001, steps=18), False),                       # 20 vs 19 turns
    ("cache_miss_spike", lambda: f_cache_miss_ctx(30000), lambda: f_cache_miss_ctx(29999), False),
    ("subagent_sprawl", lambda: f_sprawl(2, n=5), lambda: f_sprawl(2, n=4), False),
    ("ghost_trace", lambda: f_ghost(50001, 2), lambda: f_ghost(50000, 2), False),
    ("session_too_long", lambda: f_session_too_long(30, 50001), lambda: f_session_too_long(30, 50000), False),
    ("recursive_spawn_risk", lambda: f_spawn([2, 3, 5]), lambda: f_spawn([2, 3, 4]), False),
    ("idle_session_cost", lambda: f_idle([601] * 3, ctx=30001), lambda: f_idle([601] * 3, ctx=30000), False),
    ("session_length_warning", lambda: f_length(50, peak=120001), lambda: f_length(50, peak=120000), True),
    # "no drop of more than 50%": a drop to exactly half is not a compaction
    ("context_growth_no_compaction", lambda: f_growth_with_drop(GROWTH_PREV // 2),
     lambda: f_growth_with_drop(GROWTH_PREV // 2 - 1), False),
    ("tool_thrashing", lambda: f_thrash_spread(8), lambda: f_thrash_spread(9), False),         # 8-turn window
    ("post_compaction_rework", lambda: f_rework_drop(44999), lambda: f_rework_drop(45000), False),  # > 50% drop
    ("post_compaction_rework", lambda: f_rework_window(8), lambda: f_rework_window(9), False),  # within 5 turns
    # old transcripts without attachment records: system tokens - 22,000 built-in floor > 5,000
    ("system_prompt_bloat", lambda: f_system(27101), lambda: f_system(27100), False),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tes-test-")
        self.root = os.path.join(self.tmp, "projects")
        os.makedirs(self.root)
        os.environ["TOKEN_EFFICIENCY_HISTORY"] = os.path.join(self.tmp, "history.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def analyze(self, tx, full=False, **kw):
        path = tx.write(self.root)
        return at.analyze_session(path, full=full, **kw)

    def signals(self, report):
        return {i["signal"] for i in report["issues"]}

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = at.main(list(argv) + ["--projects-dir", self.root])
        return code, json.loads(buf.getvalue())


class TestParsing(Base):
    def test_streaming_chunks_count_once(self):
        """A.10.1: two chunks of one message.id count once (last record wins)."""
        t = Tx().user()
        ids = t.call(ctx=5000, inp=10, cr=4000, cw=990, out=300, thinking="plan",
                     tools=[("Read", {"file_path": "/a"})], chunks=True)
        t.results(ids, ["ok"])
        self.assertEqual(sum(1 for r in t.records if r["type"] == "assistant"), 2)
        r = self.analyze(t)
        ts = r["token_summary"]
        self.assertEqual(r["session"]["model_calls"], 1)
        self.assertEqual(ts["input_tokens"], 10)
        self.assertEqual(ts["cache_read_tokens"], 4000)
        self.assertEqual(ts["cache_write_tokens"], 990)
        self.assertEqual(ts["output_tokens"], 300)   # last chunk, not 8 + 300
        self.assertEqual(r["tool_summary"]["total_calls"], 1)

    def test_content_blocks_merged_across_chunks(self):
        """Real transcripts put each content block in its own chunk: keep every tool_use."""
        t = Tx().user()
        ids = t.call(tools=[("Read", {"file_path": "/a"}), ("Read", {"file_path": "/b"}),
                            ("Grep", {"pattern": "x"})], chunks=True)
        t.results(ids, ["1", "2", "3"], split=True)
        r = self.analyze(t)
        self.assertEqual(r["session"]["model_calls"], 1)
        self.assertEqual(r["tool_summary"]["total_calls"], 3)
        self.assertEqual(r["tool_summary"]["by_tool"]["Read"]["calls"], 2)

    def test_cache_hit_ratio_hand_calculation(self):
        """A.10.2: cache_read / (cache_read + cache_write + input)."""
        t = Tx(model="claude-opus-5-5").user()
        t.step([BASH], inp=10, cr=0, cw=20000, out=100)
        t.step([("Bash", {"command": "ls"})], inp=5, cr=20000, cw=3000, out=200)
        t.call(inp=3, cr=23000, cw=1000, out=400, text="done")
        r = self.analyze(t)
        ts = r["token_summary"]
        hand = 43000 / (43000 + 24000 + 18)
        self.assertAlmostEqual(ts["cache_hit_ratio"], hand, places=6)
        self.assertEqual(ts["max_context_tokens"], 24003)
        # opus-5-5: input 4, output 20, cache_read 0.20, cache_create 5.00 per 1M
        cost = (18 * 4 + 700 * 20 + 43000 * 0.20 + 24000 * 5.00) / 1e6
        self.assertAlmostEqual(ts["estimated_cost_usd"], cost, places=6)
        self.assertEqual(ts["pricing_model"], "opus-5-5")

    def test_each_user_record_starts_a_turn_and_results_map_across_turns(self):
        t = Tx().user()
        ids = t.call(tools=[("Bash", {"command": "a"}), ("Edit", {"file_path": "/x", "old_string": "1",
                                                                   "new_string": "2"})])
        t.results(ids, ["ok", {"content": "no match", "is_error": True}], split=True)
        t.call(text="done")
        r = self.analyze(t)
        self.assertEqual(r["session"]["turns"], 3)
        self.assertEqual(r["tool_summary"]["by_tool"]["Edit"]["errors"], 1)
        self.assertEqual(r["tool_summary"]["by_tool"]["Bash"]["errors"], 0)

    def test_bad_lines_skipped(self):
        t = Tx().user()
        t.call(text="hi")
        path = t.write(self.root)
        with open(path, "a") as fh:
            fh.write("\n{not json\n   \n")
        r = at.analyze_session(path)
        self.assertEqual(r["session"]["model_calls"], 1)

    def test_system_tokens_and_estimated_output(self):
        t = Tx().user("u" * 4000)
        t.call(ctx=21000, out=10, text="z" * 400)
        ts = self.analyze(t)["token_summary"]
        self.assertEqual(ts["system_tokens"], 20000)
        self.assertEqual(ts["estimated_output_tokens"], 100)

    def test_waste_type_prices(self):
        """Carried-forward context is priced at cache_read, not cache_create."""
        r = self.analyze(f_context_growth(110000))
        iss = [i for i in r["issues"] if i["signal"] == "context_growth_no_compaction"][0]
        self.assertEqual(iss["waste_type"], "cache_read")
        self.assertAlmostEqual(iss["cost_wasted_usd"], iss["tokens_wasted"] / 1e6 * 0.20, places=6)


class TestDetectors(Base):
    def test_all_23_detectors_have_cases(self):
        self.assertEqual(len(at.DETECTORS), 23)
        self.assertEqual(set(CASES), {d[0] for d in at.DETECTORS})

    def test_trigger_and_just_under(self):
        """A.10.3: each detector fires on its fixture and not just under the threshold."""
        for signal, (trigger, under, full) in sorted(CASES.items()):
            with self.subTest(signal=signal):
                r = self.analyze(trigger(), full=full)
                self.assertIn(signal, self.signals(r), "trigger fixture did not fire")
                iss = [i for i in r["issues"] if i["signal"] == signal][0]
                for k in ("signal", "severity", "description", "turns", "tokens_wasted", "waste_type",
                          "cost_wasted_usd", "recommendation"):
                    self.assertIn(k, iss)
                self.assertIn(iss["waste_type"], ("input", "output", "cache_read", "cache_create"))
                self.assertTrue(iss["turns"], "issue has no turn numbers")
                r2 = self.analyze(under(), full=full)
                self.assertNotIn(signal, self.signals(r2), "fired just under the threshold")

    def test_second_conditions(self):
        for n, (signal, trigger, under, full) in enumerate(EXTRA_CASES):
            with self.subTest(signal=signal, case=n):
                self.assertIn(signal, self.signals(self.analyze(trigger(), full=full)))
                self.assertNotIn(signal, self.signals(self.analyze(under(), full=full)))

    def test_full_adds_exactly_the_two_info_detectors(self):
        """A.10.4."""
        default = {d[0] for d in at.active_detectors(False)}
        full = {d[0] for d in at.active_detectors(True)}
        self.assertEqual(len(default), 21)
        self.assertEqual(full - default, {"thinking_overhead", "session_length_warning"})
        self.assertTrue(default < full)
        t = f_length(50)
        t.step([("Bash", {"command": "ls"})], thinking="t" * 3000, ctx=60000)
        r0, r1 = self.analyze(t), self.analyze(t, full=True)
        self.assertEqual(r0["detectors_run"], 21)
        self.assertEqual(r1["detectors_run"], 23)
        self.assertEqual(self.signals(r1) - self.signals(r0), {"thinking_overhead", "session_length_warning"})
        self.assertFalse(any(i["severity"] == "INFO" for i in r0["issues"]))

    def test_image_results_are_marked(self):
        t = Tx().user()
        t.step([("Read", {"file_path": "/repo/shot.png"})],
               results=[[{"type": "image", "source": {"type": "base64", "data": "A" * 60000}}]])
        iss = [i for i in self.analyze(t)["issues"] if i["signal"] == "large_tool_result"][0]
        self.assertIn("image", iss["description"])


class TestModes(Base):
    def _corpus(self):
        a = f_context_growth(110000)
        a.sid = "aaaa1111"
        a.write(self.root, project="-Users-me-Projects-video-editor")
        b = f_mismatch(5)
        b.sid = "bbbb2222"
        b.write(self.root, project="-Users-me-Projects-video-editor")
        s = f_error(True)
        s.write(self.root, project="-Users-me-Projects-video-editor", name="agent-a1", subagent_of="aaaa1111")
        o = f_error(True)
        o.sid = "cccc3333"
        o.write(self.root, project="-Users-me-Projects-video-editor-pm")

    def test_aggregate_counts_sessions_and_subagents_separately(self):
        self._corpus()
        code, out = self.run_cli("--last", "30d", "--aggregate", "--project", "video_editor", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(out["transcripts"], {"total": 3, "sessions": 2, "subagents": 1})
        sig = {s["signal"]: s for s in out["signals"]}
        self.assertEqual(sig["wasted_call_error"]["frequency"], "0/2 sessions, 1/1 subagents")
        self.assertIn("total_cost_wasted_usd", out)
        self.assertIn("total_tokens_wasted", out)

    def test_aggregate_counts_image_results(self):
        t = Tx(sid="dddd4444").user()
        t.step([("Read", {"file_path": "/repo/shot.png"})],
               results=[[{"type": "image", "source": {"type": "base64", "data": "A" * 60000}}]])
        t.step([("Bash", {"command": "cat big.log"})], results=["x" * 60000])
        t.write(self.root, project="-Users-me-Projects-shots")
        code, out = self.run_cli("--last", "30d", "--aggregate", "--project", "shots")
        sig = {s["signal"]: s for s in out["signals"]}
        self.assertEqual(sig["large_tool_result"]["issues"], 2)
        self.assertEqual(sig["large_tool_result"]["image_or_pdf_issues"], 1)
        self.assertEqual(sig["oversized_read"]["image_or_pdf_issues"], 1)

    def test_window_uses_mtime(self):
        self._corpus()
        old = time.time() - 40 * 86400
        for p in glob.glob(os.path.join(self.root, "*", "bbbb2222.jsonl")):
            os.utime(p, (old, old))
        code, out = self.run_cli("--last", "30d", "--aggregate", "--project", "video_editor")
        self.assertEqual(out["transcripts"]["sessions"], 1)

    def test_compare_gives_deltas(self):
        self._corpus()
        code, out = self.run_cli("--compare", "aaaa1111", "bbbb2222")
        self.assertEqual(code, 0)
        for k in ("estimated_cost_usd", "cache_hit_ratio", "max_context_tokens"):
            self.assertIn(k, out["delta"])
        self.assertAlmostEqual(out["delta"]["max_context_tokens"],
                               out["b"]["token_summary"]["max_context_tokens"]
                               - out["a"]["token_summary"]["max_context_tokens"])

    def test_session_lookup_turn_and_detail(self):
        self._corpus()
        code, out = self.run_cli("--session", "aaaa1111", "--turn", "3", "--detail")
        self.assertEqual(code, 0)
        self.assertEqual(out["turn_detail"]["turn"], 3)
        self.assertEqual(len(out["turns_detail"]), out["session"]["turns"])
        code, out = self.run_cli("--session", "agent-a1")
        self.assertEqual(out["session"]["kind"], "subagent")
        code, out = self.run_cli("--session", "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("error", out)

    def test_current_session_from_env(self):
        self._corpus()
        os.environ["CLAUDE_SESSION_ID"] = "bbbb2222"
        try:
            code, out = self.run_cli("--current")
        finally:
            del os.environ["CLAUDE_SESSION_ID"]
        self.assertEqual(out["session"]["id"], "bbbb2222")

    def test_history_uses_env_path(self):
        self._corpus()
        hist = os.environ["TOKEN_EFFICIENCY_HISTORY"]
        self.run_cli("--session", "aaaa1111", "--save-history")
        self.run_cli("--session", "bbbb2222", "--save-history")
        with open(hist) as fh:
            self.assertEqual(len(fh.readlines()), 2)
        code, out = self.run_cli("--history")
        self.assertEqual(len(out["entries"]), 2)
        self.assertIn("trend", out)

    def test_recommendations_sorted_and_one_per_signal(self):
        t = f_exploration(20)
        t.step([("Read", {"file_path": "/repo/big.txt"})], results=["q" * 60000])
        r = self.analyze(t)
        recs = r["recommendations"]
        self.assertEqual(len(recs), len({x["signal"] for x in recs}))
        saved = [x["estimated_tokens_saved"] for x in recs]
        self.assertEqual(saved, sorted(saved, reverse=True))
        for x in recs:
            self.assertIn("estimated_savings_pct", x)
            self.assertIn("estimated_savings_usd", x)


class TestReadOnly(Base):
    def test_fixture_bytes_unchanged(self):
        path = f_rework(["/repo/a.py", "/repo/b.py"]).write(self.root)
        def snap():
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest(), os.stat(path).st_mtime_ns
        before = snap()
        at.analyze_session(path, full=True, detail=True, turn=2)
        after = snap()
        self.assertEqual(before, after)

    def test_real_transcripts_unchanged(self):
        """A.10.5 on real files: size and mtime stay the same."""
        real = os.path.expanduser("~/.claude/projects")
        files = glob.glob(os.path.join(real, "*", "*.jsonl")) + \
            glob.glob(os.path.join(real, "*", "*", "subagents", "*.jsonl"))
        cutoff = time.time() - 3600   # skip files that may be live
        files = [f for f in files if os.path.getmtime(f) < cutoff]
        if not files:
            self.skipTest("no real transcripts on this machine")
        files.sort(key=os.path.getsize)
        pick = files[:6] + files[len(files) // 2:len(files) // 2 + 2] + files[-1:]
        before = {f: (os.stat(f).st_size, os.stat(f).st_mtime_ns) for f in pick}
        for f in pick:
            at.analyze_session(f, full=True, detail=True)
        after = {f: (os.stat(f).st_size, os.stat(f).st_mtime_ns) for f in pick}
        self.assertEqual(before, after)


class TestFixesV11(Base):
    """v1.1: the three analyzer flaws found in the before/after experiment, plus cache economics."""

    # Flaw 1: system_prompt_bloat fired on every run, even after the prefix was trimmed.
    def test_bloat_ignores_builtin_prompt_and_claude_md(self):
        lean = Tx().user("fix the test")
        lean.attach("instructions", "c" * 30000)           # CLAUDE.md, 7,500 tokens
        lean.attach("agent_listing_delta", "a" * 2000)     # built-in agents only
        lean.call(ctx=35000, text="ok")                    # 35K system tokens: the old 15K rule fired
        r = self.analyze(lean)
        self.assertGreater(r["token_summary"]["system_tokens"], 15000)
        self.assertNotIn("system_prompt_bloat", self.signals(r))
        self.assertEqual(r["token_summary"]["optional_prefix_tokens"], 500)
        full = Tx().user("fix the test")
        full.attach("instructions", "c" * 30000)
        full.attach("agent_listing_delta", "a" * 2000)
        full.attach("skill_listing", "s" * 40000)
        full.attach("deferred_tools_delta", "d" * 20000)
        full.call(ctx=50000, text="ok")
        iss = [i for i in self.analyze(full)["issues"] if i["signal"] == "system_prompt_bloat"]
        self.assertEqual(len(iss), 1)
        self.assertIn("skill_listing", iss[0]["description"])
        self.assertIn("15,500 optional prefix tokens", iss[0]["description"])

    def test_bloat_counts_listings_injected_after_the_first_call(self):
        t = Tx().user("go")
        t.attach("agent_listing_delta", "a" * 2000)
        t.step([BASH], ctx=30000)
        t.attach("skill_listing", "s" * 40000)
        t.attach("mcp_instructions_delta", "m" * 8000)
        t.call(ctx=42000, text="done")
        self.assertIn("system_prompt_bloat", self.signals(self.analyze(t)))

    # Flaw 2: exploration_overhead missed Bash grep/find/rg exploration.
    def test_bash_explore_ops(self):
        ops = at.bash_explore_ops
        self.assertEqual(ops("cd /repo && ls -la && cat package.json && grep -rn foo src | head -5"), 3)
        self.assertEqual(ops("rg -n 'a|b' lib; find . -name '*.ts' | wc -l"), 2)
        self.assertEqual(ops("git -C /repo log --oneline -5 && git status && git commit -m x"), 1)
        self.assertEqual(ops("sed -n 1,40p a.py && sed -i 's/a/b/' a.py"), 1)
        self.assertEqual(ops("cat > notes.md <<EOF"), 0)
        self.assertEqual(ops("npm test && npx vitest run a.test.ts"), 0)
        self.assertEqual(ops("FOO=1 grep -c x y"), 1)

    def test_exploration_counts_shell_reads(self):
        def build(n_calls):
            t = Tx().user()
            for i in range(n_calls):
                t.step([("Bash", {"command": "cd /repo && ls src%d && cat a%d.ts && grep -rn x%d lib | head" % (i, i, i)})],
                       results=[lines(3)])
            t.step([("Edit", {"file_path": "/repo/a.ts", "old_string": "a", "new_string": "b"})])
            return t
        r = self.analyze(build(5))                          # 5 calls x 3 commands = 15
        iss = [i for i in r["issues"] if i["signal"] == "exploration_overhead"]
        self.assertEqual(len(iss), 1)
        self.assertIn("read-only shell commands 15", iss[0]["description"])
        self.assertNotIn("exploration_overhead", self.signals(self.analyze(build(4))))   # 12

    # Flaw 3: waste dollars added up to 1.45-2.07 x the real cost.
    def _busy(self):
        t = Tx().user("x")
        t.attach("skill_listing", "s" * 60000)
        for i in range(24):
            t.step([("Read", {"file_path": "/repo/big%d.txt" % (i % 3)})], ctx=40000 + i * 4000, hit=0.97,
                   results=["y" * 60000])
        t.step([BASH], inp=2, cr=0, cw=140000)              # cache miss on a big context
        for i in range(6):
            t.step([("Bash", {"command": "ls %d" % i})], ctx=141000 + i * 100, hit=0.99)
        return t

    def test_waste_dollars_never_overlap_or_exceed_cost(self):
        r = self.analyze(self._busy(), full=True)
        cost = r["token_summary"]["estimated_cost_usd"]
        naive = sum(i["tokens_wasted"] / 1e6 * at.PRICES["opus-5-5"][i["waste_type"]] for i in r["issues"])
        self.assertGreater(naive, cost)                     # the v1.0 sum was above the real cost
        waste = sum(i["cost_wasted_usd"] for i in r["issues"])
        breaks = r["cache_economics"]["breaks"]["lost_usd"]
        self.assertLessEqual(waste + breaks, cost + 1e-6)
        self.assertAlmostEqual(r["total_cost_wasted_usd"], waste, places=5)
        self.assertLessEqual(r["waste_to_cost_ratio"], 1.0)
        self.assertGreater(waste, 0)

    def test_aggregate_waste_ratio_at_most_one(self):
        t = self._busy()
        t.sid = "eeee5555"
        t.write(self.root, project="-Users-me-Projects-busy")
        code, out = self.run_cli("--last", "30d", "--aggregate", "--project", "busy", "--full")
        self.assertEqual(code, 0)
        self.assertLessEqual(out["waste_to_cost_ratio"], 1.0)
        self.assertLessEqual(sum(s["cost_wasted_usd"] for s in out["signals"]),
                             out["token_summary"]["estimated_cost_usd"] + 1e-6)

    # Cache economics
    def test_1h_cache_writes_and_no_cache_counterfactual(self):
        t = Tx(model="claude-opus-5-5").user()
        t.call(inp=10, cr=0, cw=10000, cw1h=10000, out=100, text="a")
        t.user("next")
        t.call(inp=10, cr=10000, cw=2000, out=100, text="b")      # unlabelled write = 5-minute
        ts = self.analyze(t)["token_summary"]
        # opus-5-5: input 4, 5m write 5, 1h write 8, read 0.20, output 20 ($/1M)
        self.assertAlmostEqual(ts["estimated_cost_usd"],
                               (20 * 4 + 10000 * 8 + 2000 * 5 + 10000 * 0.20 + 200 * 20) / 1e6, places=6)
        self.assertEqual(ts["cache_write_1h_tokens"], 10000)
        self.assertEqual(ts["cache_write_5m_tokens"], 2000)
        self.assertAlmostEqual(ts["no_cache_cost_usd"], ((20 + 12000 + 10000) * 4 + 200 * 20) / 1e6, places=6)

    def test_cache_break_causes(self):
        t = Tx(model="claude-opus-5-5").user()
        t.call(inp=2, cr=0, cw=50000, cw1h=50000, out=10, text="a")
        t.user("back after lunch", gap=7200)
        t.call(inp=2, cr=0, cw=50100, cw1h=50100, out=10, text="b")              # idle over 1 hour
        t.user("again", gap=30)
        t.call(inp=2, cr=0, cw=50200, cw1h=50200, out=10, text="c")               # prompt changed
        t.user("switch", gap=30)
        t.call(inp=2, cr=0, cw=50300, cw1h=50300, out=10, text="d", model="claude-sonnet-5-5")  # model switch
        ce = self.analyze(t)["cache_economics"]
        causes = [b["cause"] for b in ce["breaks"]["items"]]
        self.assertEqual(causes, ["idle_over_1h", "prompt_changed", "model_switch"])
        first = ce["breaks"]["items"][0]
        self.assertEqual(first["lost_tokens"], 50002)
        self.assertAlmostEqual(first["lost_usd"], 50002 * (8 - 0.20) / 1e6, places=6)

    def _subagent_with_ttl_break(self):
        t = Tx(model="claude-opus-5-5").user("verify")
        t.step([("Bash", {"command": "npm run check"})], inp=2, cr=0, cw=40000, out=10)
        ids = t.call(inp=2, cr=40000, cw=1000, out=10, tools=[("Bash", {"command": "npm run lint"})])
        t.results(ids, ["ok"], gap=420)                                          # 7-minute tool run
        t.call(inp=2, cr=0, cw=41100, out=10, text="done", gap=1)
        return t

    def test_ttl_5m_expiry_and_1h_simulation(self):
        t = self._subagent_with_ttl_break()
        path = t.write(self.root, name="agent-x1", subagent_of="ffff6666")
        r = at.analyze_session(path)
        ce = r["cache_economics"]
        self.assertEqual(list(ce["breaks"]["by_cause"]), ["ttl_5m_expiry"])
        lost = 41002                                                             # min(write, previous context)
        self.assertAlmostEqual(ce["breaks"]["lost_usd"], lost * (5 - 0.20) / 1e6, places=6)
        sim = ce["ttl_1h_simulation"]
        writes_5m = 40000 + 1000 + 41100
        self.assertEqual(sim["breaks_avoided"], 1)
        self.assertAlmostEqual(sim["extra_write_usd"], (writes_5m - lost) * (8 - 5) / 1e6, places=6)
        self.assertAlmostEqual(sim["delta_usd"], ((writes_5m - lost) * 8 + lost * 0.20) / 1e6
                               - (writes_5m * 5) / 1e6, places=6)

    def test_dashboard_is_self_contained_and_has_no_ids(self):
        t = self._subagent_with_ttl_break()
        t.write(self.root, project="-Users-me-Projects-dash", name="agent-x1", subagent_of="ffff6666")
        m = f_context_growth(110000)
        m.sid = "ffff6666-aaaa-bbbb-cccc-000000000001"
        m.user("SECRET PROMPT TEXT about /Users/me/private/file.ts")
        m.call(text="ok")
        m.write(self.root, project="-Users-me-Projects-dash")
        dash = os.path.join(self.tmp, "out", "dashboard.html")
        code, out = self.run_cli("--last", "30d", "--aggregate", "--project", "dash", "--dashboard", dash)
        self.assertEqual(code, 0)
        self.assertEqual(out["dashboard"], os.path.abspath(dash))
        with open(dash, encoding="utf-8") as fh:
            page = fh.read()
        for needle in ("cache가 없었다면", "cache가 깨진 곳", "낭비 패턴", "ROI", "추천 3가지", "5분 TTL 만료"):
            self.assertIn(needle, page)
        for banned in ("http://", "https://", "<script", "<link", "@import", "ffff6666", "SECRET",
                       "/Users/", "/tmp/demo-proj", "agent-x1"):
            self.assertNotIn(banned, page)
        self.run_cli("--last", "30d", "--aggregate", "--project", "dash", "--dashboard", dash, "--dashboard-ids")
        with open(dash, encoding="utf-8") as fh:
            self.assertIn("ffff6666", fh.read())

    def test_actions_rank_by_roi_and_scale_to_30_days(self):
        t = self._subagent_with_ttl_break()
        t.write(self.root, project="-Users-me-Projects-roi", name="agent-x1", subagent_of="ffff6666")
        code, out = self.run_cli("--last", "15d", "--aggregate", "--project", "roi")
        acts = {a["id"]: a for a in out["actions"]}
        self.assertNotIn("main_ttl_1h", acts)               # no main-session 5-minute writes
        ttl = acts["subagent_ttl_1h"]
        sim = out["cache_economics"]["by_kind"]["subagent"]["ttl_1h_simulation"]
        self.assertAlmostEqual(ttl["usd_window"], round(-sim["delta_usd"], 2), places=2)
        self.assertAlmostEqual(ttl["usd_30d"], round(-sim["delta_usd"] * 2, 2), places=2)
        rois = [a["roi_usd_per_hour"] for a in out["actions"]]
        self.assertEqual(rois, sorted(rois, reverse=True))
        self.assertTrue(set(out["top_actions"]) <= set(acts))


if __name__ == "__main__":
    unittest.main()
