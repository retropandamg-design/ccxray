#!/usr/bin/env python3
"""
ccxray - an X-ray of your Claude Code usage.

Reads the session transcripts Claude Code already writes to ~/.claude/projects
and tells you what they actually cost, where the money went, and how much of it
was wasted on rework.

Zero dependencies. Single file. Nothing leaves your machine.

    python3 ccxray.py              # terminal report
    python3 ccxray.py --html o.html  # shareable HTML report
    python3 ccxray.py --json        # machine-readable

MIT licensed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------
# USD per million tokens, (input, output). Cache reads bill at 0.1x input,
# cache writes at 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL.
PRICING = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Claude Fable 5.1 reads cache at 0.025x rather than the usual 0.1x.
CACHE_READ_MULTIPLIER = defaultdict(lambda: 0.1, {"claude-fable-5-1": 0.025})
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0

DEFAULT_PRICE = (5.0, 25.0)  # unknown model: assume Opus tier


def price_for(model):
    return PRICING.get(model, DEFAULT_PRICE)


def cost_of(model, inp, out, cache_read, c5m, c1h):
    """Dollar cost of one API response, at published list prices."""
    p_in, p_out = price_for(model)
    read_mult = CACHE_READ_MULTIPLIER[model]
    return (
        inp * p_in
        + out * p_out
        + cache_read * p_in * read_mult
        + c5m * p_in * CACHE_WRITE_5M
        + c1h * p_in * CACHE_WRITE_1H
    ) / 1_000_000.0


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

class Bucket:
    """A running tally of tokens and dollars."""

    __slots__ = ("inp", "out", "thinking", "cache_read", "c5m", "c1h", "cost", "responses")

    def __init__(self):
        self.inp = self.out = self.thinking = 0
        self.cache_read = self.c5m = self.c1h = 0
        self.cost = 0.0
        self.responses = 0

    def add(self, inp, out, thinking, cache_read, c5m, c1h, cost):
        self.inp += inp
        self.out += out
        self.thinking += thinking
        self.cache_read += cache_read
        self.c5m += c5m
        self.c1h += c1h
        self.cost += cost
        self.responses += 1

    @property
    def total_tokens(self):
        return self.inp + self.out + self.cache_read + self.c5m + self.c1h

    def as_dict(self):
        return {
            "input_tokens": self.inp,
            "output_tokens": self.out,
            "thinking_tokens": self.thinking,
            "cache_read_tokens": self.cache_read,
            "cache_write_5m_tokens": self.c5m,
            "cache_write_1h_tokens": self.c1h,
            "total_tokens": self.total_tokens,
            "responses": self.responses,
            "cost_usd": round(self.cost, 4),
        }


class Session:
    def __init__(self, sid, project):
        self.sid = sid
        self.project = project
        self.bucket = Bucket()
        self.first_ts = None
        self.last_ts = None
        self.title = None
        self.first_prompt = None
        self.user_turns = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.rework = 0
        self.turns = []  # (context_tokens_read, cost) in order, for the cost curve

    def touch(self, ts):
        if not ts:
            return
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = ts

    @property
    def duration_min(self):
        if not self.first_ts or not self.last_ts:
            return 0.0
        return max(0.0, (parse_ts(self.last_ts) - parse_ts(self.first_ts)).total_seconds() / 60.0)

    def label(self):
        if self.title:
            return self.title
        if self.first_prompt:
            return squash(self.first_prompt, 60)
        return self.sid[:8]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def squash(text, limit):
    text = _WS.sub(" ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_ts(ts):
    if not ts:
        return _dt.datetime.min
    try:
        return _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return _dt.datetime.min


def project_name(path):
    """Fallback name, used only until a record tells us the real cwd.

    Claude Code flattens the project path into a directory name by replacing
    every separator with a dash, which is lossy: dashes, underscores and spaces
    in the original path all collapse to the same character. We prefer the cwd
    recorded inside the transcript and fall back to this.
    """
    base = os.path.basename(os.path.dirname(path))
    return base.lstrip("-").split("-")[-1] or base


def label_for_cwd(cwd):
    """A short, recognisable name for a working directory."""
    cwd = (cwd or "").rstrip("/")
    if not cwd:
        return None
    parts = [p for p in cwd.split("/") if p]
    if not parts:
        return None
    # Two trailing segments read better than one ("acme-api/workers").
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


def text_of(content):
    """Flatten a message content field to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return " ".join(out)


def fingerprint(tool_name, tool_input):
    """A stable signature for 'the agent tried this exact thing again'."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash":
        cmd = _WS.sub(" ", (tool_input.get("command") or "").strip())
        return ("Bash", cmd[:400]) if cmd else None
    for key in ("file_path", "path", "pattern", "url", "query"):
        if tool_input.get(key):
            return (tool_name, str(tool_input[key])[:400])
    return None


def is_error_result(payload):
    """Did this tool result come back as a failure?"""
    if isinstance(payload, dict):
        if payload.get("is_error") or payload.get("isError"):
            return True
        text = payload.get("content")
        if isinstance(text, str) and text.startswith("Error:"):
            return True
    return False


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

class Analysis:
    def __init__(self):
        self.total = Bucket()
        self.by_project = defaultdict(Bucket)
        self.by_model = defaultdict(Bucket)
        self.by_day = defaultdict(Bucket)
        self.sessions = {}
        self.tools = Counter()
        self.tool_errors = Counter()
        self.error_messages = Counter()
        self.rework_commands = Counter()
        self.files_touched = Counter()
        self.duplicate_responses = 0
        self.duplicate_cost = 0.0
        self.lines_read = 0
        self.files_read = 0
        self.parse_failures = 0


def scan(paths, verbose=False):
    a = Analysis()
    seen_message_ids = set()

    for path in paths:
        a.files_read += 1
        project = project_name(path)
        resolved_project = False
        sid = os.path.splitext(os.path.basename(path))[0]
        session = a.sessions.get(sid)
        if session is None:
            session = a.sessions[sid] = Session(sid, project)

        # tool_use_id -> (tool name, fingerprint) so a later result can be attributed
        pending = {}
        recent_fingerprints = []

        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            if verbose:
                print("  skipped %s (%s)" % (path, exc), file=sys.stderr)
            continue

        with handle:
            for line in handle:
                a.lines_read += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    a.parse_failures += 1
                    continue
                if not isinstance(rec, dict):
                    continue

                kind = rec.get("type")
                ts = rec.get("timestamp")
                session.touch(ts)

                if not resolved_project:
                    better = label_for_cwd(rec.get("cwd"))
                    if better:
                        project = better
                        session.project = better
                        resolved_project = True

                if kind == "custom-title" and rec.get("customTitle"):
                    session.title = rec["customTitle"]
                    continue

                message = rec.get("message")
                if not isinstance(message, dict):
                    message = {}

                if kind == "user":
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        session.user_turns += 1
                        if session.first_prompt is None:
                            session.first_prompt = content
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_result" and is_error_result(block):
                                name = (pending.get(block.get("tool_use_id")) or ("?", None))[0]
                                a.tool_errors[name] += 1
                                session.tool_errors += 1
                                snippet = block.get("content")
                                if isinstance(snippet, str):
                                    a.error_messages[squash(snippet, 100)] += 1
                    result = rec.get("toolUseResult")
                    if is_error_result(result):
                        a.tool_errors["(result)"] += 1
                        session.tool_errors += 1
                    continue

                if kind != "assistant":
                    continue

                # ---- tool calls -------------------------------------------
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or "?"
                    tool_input = block.get("input")
                    a.tools[name] += 1
                    session.tool_calls += 1
                    fp = fingerprint(name, tool_input)
                    pending[block.get("id")] = (name, fp)
                    if isinstance(tool_input, dict):
                        fpath = tool_input.get("file_path")
                        if fpath and name in ("Edit", "Write", "NotebookEdit"):
                            a.files_touched[fpath] += 1
                    if fp:
                        if fp in recent_fingerprints:
                            session.rework += 1
                            if fp[0] == "Bash":
                                a.rework_commands[squash(fp[1], 90)] += 1
                        recent_fingerprints.append(fp)
                        if len(recent_fingerprints) > 40:
                            recent_fingerprints.pop(0)

                # ---- usage / cost -----------------------------------------
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue

                model = message.get("model") or "unknown"
                cache_creation = usage.get("cache_creation") or {}
                inp = usage.get("input_tokens") or 0
                out = usage.get("output_tokens") or 0
                cache_read = usage.get("cache_read_input_tokens") or 0
                c5m = cache_creation.get("ephemeral_5m_input_tokens") or 0
                c1h = cache_creation.get("ephemeral_1h_input_tokens") or 0
                if not c5m and not c1h:
                    c5m = usage.get("cache_creation_input_tokens") or 0
                details = usage.get("output_tokens_details") or {}
                thinking = details.get("thinking_tokens") or 0

                cost = cost_of(model, inp, out, cache_read, c5m, c1h)

                # Claude Code copies earlier turns forward when a session is
                # resumed or branched, so the same API response can appear in
                # several files. Counting it twice inflates the bill.
                mid = message.get("id")
                if mid:
                    if mid in seen_message_ids:
                        a.duplicate_responses += 1
                        a.duplicate_cost += cost
                        continue
                    seen_message_ids.add(mid)

                if model == "<synthetic>":
                    continue

                args = (inp, out, thinking, cache_read, c5m, c1h, cost)
                a.total.add(*args)
                a.by_project[project].add(*args)
                a.by_model[model].add(*args)
                session.bucket.add(*args)
                session.turns.append((cache_read, cost))
                if ts:
                    a.by_day[ts[:10]].add(*args)

    return a


# --------------------------------------------------------------------------
# Cost curve
# --------------------------------------------------------------------------

def cost_curve(a, width=25, min_len=40, min_samples=15):
    """How the price of one response changes as a session gets longer.

    Every turn re-sends the conversation so far, so the context a response has
    to read grows monotonically. Bucketing responses by their position in the
    session shows what that costs.
    """
    buckets = defaultdict(lambda: [0, 0.0, 0])
    for s in a.sessions.values():
        if len(s.turns) < min_len:
            continue
        for i, (ctx, cost) in enumerate(s.turns):
            b = buckets[(i // width) * width]
            b[0] += ctx
            b[1] += cost
            b[2] += 1
    rows = []
    for start in sorted(buckets):
        ctx, cost, n = buckets[start]
        if n < min_samples:
            continue
        rows.append({
            "turn_start": start,
            "turn_end": start + width - 1,
            "responses": n,
            "avg_context_tokens": int(ctx / n),
            "avg_cost_usd": round(cost / n, 5),
        })
    return rows


# --------------------------------------------------------------------------
# Derived findings
# --------------------------------------------------------------------------

def findings(a):
    """The handful of things actually worth acting on."""
    out = []
    total_cost = a.total.cost
    if total_cost <= 0:
        return out

    p_read = 0.0
    for model, b in a.by_model.items():
        p_in, _ = price_for(model)
        p_read += b.cache_read * p_in * CACHE_READ_MULTIPLIER[model] / 1_000_000.0
    share = p_read / total_cost * 100
    if share >= 40:
        out.append((
            "Context re-reading is your biggest line item",
            "%.0f%% of your spend ($%.2f) is cache reads — paying to re-send the same "
            "context on every turn. Shorter sessions and tighter CLAUDE.md files hit this directly."
            % (share, p_read),
        ))

    c1h, c5m = a.total.c1h, a.total.c5m
    if c1h and c1h > c5m:
        extra = 0.0
        for model, b in a.by_model.items():
            p_in, _ = price_for(model)
            extra += b.c1h * p_in * (CACHE_WRITE_1H - CACHE_WRITE_5M) / 1_000_000.0
        out.append((
            "Cache writes used the 1-hour TTL",
            "1-hour cache writes bill at 2x input vs 1.25x for the 5-minute TTL — a $%.2f "
            "premium here. Claude Code picks the TTL, not you, so this is a cost to know "
            "about rather than a setting to change: it pays for itself only when you come "
            "back to a session within the hour instead of starting fresh." % extra,
        ))

    calls = sum(a.tools.values())
    errs = sum(a.tool_errors.values())
    if calls and errs / calls >= 0.03:
        out.append((
            "%.1f%% of tool calls failed" % (errs / calls * 100),
            "%d of %d tool calls came back as errors. Every failure is a full round trip "
            "you paid for and got nothing from." % (errs, calls),
        ))

    rework = sum(s.rework for s in a.sessions.values())
    if rework >= 10:
        out.append((
            "%d repeated attempts detected" % rework,
            "The same command or file was retried after already being tried in the same "
            "session — the signature of a trial-and-error loop rather than a fix.",
        ))

    if a.duplicate_responses:
        out.append((
            "%d duplicated records skipped" % a.duplicate_responses,
            "Resumed sessions copy earlier turns into new files. Counting them naively would "
            "have overstated your bill by $%.2f (%.0f%%)."
            % (a.duplicate_cost, a.duplicate_cost / total_cost * 100),
        ))

    curve = cost_curve(a)
    if len(curve) >= 3:
        first, last = curve[0], curve[-1]
        if first["avg_cost_usd"] > 0:
            mult = last["avg_cost_usd"] / first["avg_cost_usd"]
            if mult >= 1.6:
                out.append((
                    "Late turns cost %.1fx what early turns cost" % mult,
                    "A response at turn %d averages $%.3f against $%.3f in the first %d turns, "
                    "because the context it re-reads grew from %s to %s tokens. The work did "
                    "not get harder — the session got longer. Finishing a task and starting "
                    "clean is the single biggest lever you control."
                    % (last["turn_start"], last["avg_cost_usd"], first["avg_cost_usd"],
                       curve[0]["turn_end"] + 1, human(first["avg_context_tokens"]),
                       human(last["avg_context_tokens"])),
                ))

    top = sorted(a.sessions.values(), key=lambda s: s.bucket.cost, reverse=True)
    if top and top[0].bucket.cost > total_cost * 0.25:
        s = top[0]
        out.append((
            "One session is %.0f%% of everything" % (s.bucket.cost / total_cost * 100),
            "“%s” cost $%.2f on its own. Long-running sessions compound: every turn "
            "re-reads the whole history." % (s.label(), s.bucket.cost),
        ))
    return out


# --------------------------------------------------------------------------
# Terminal rendering
# --------------------------------------------------------------------------

class Style:
    def __init__(self, enabled):
        self.on = enabled

    def _w(self, code, text):
        return "\033[%sm%s\033[0m" % (code, text) if self.on else text

    def bold(self, t):
        return self._w("1", t)

    def dim(self, t):
        return self._w("2", t)

    def cyan(self, t):
        return self._w("36", t)

    def green(self, t):
        return self._w("32", t)

    def yellow(self, t):
        return self._w("33", t)

    def red(self, t):
        return self._w("31", t)


def human(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return "%.1f%s" % (n / size, unit)
    return str(int(n))


def bar(fraction, width=22):
    filled = int(round(fraction * width))
    return "█" * filled + "·" * (width - filled)


def render_terminal(a, st, top_n=8):
    L = []
    add = L.append
    total = a.total

    add("")
    add(st.bold("  ccxray") + st.dim("  —  what your Claude Code sessions actually cost"))
    add("")

    if total.responses == 0:
        add("  No usage data found.")
        add("")
        return "\n".join(L)

    days = sorted(a.by_day)
    span = "%s → %s" % (days[0], days[-1]) if days else "—"

    add("  " + st.dim("─" * 62))
    n = len(a.sessions)
    add("  %s   %s" % (
        st.bold(st.green("$%s" % format(total.cost, ",.2f"))),
        st.dim("across %d session%s, %s tokens, %s"
               % (n, "" if n == 1 else "s", human(total.total_tokens), span)),
    ))
    add("  " + st.dim("─" * 62))
    add("")

    # ---- where the money goes ---------------------------------------------
    p_in_read = 0.0
    p_write = 0.0
    for model, b in a.by_model.items():
        p_in, _ = price_for(model)
        p_in_read += b.cache_read * p_in * CACHE_READ_MULTIPLIER[model] / 1e6
        p_write += (b.c5m * CACHE_WRITE_5M + b.c1h * CACHE_WRITE_1H) * p_in / 1e6
    p_out = sum(b.out * price_for(m)[1] for m, b in a.by_model.items()) / 1e6
    p_fresh = sum(b.inp * price_for(m)[0] for m, b in a.by_model.items()) / 1e6

    add("  " + st.bold("WHERE THE MONEY GOES"))
    for label, value in (
        ("cache reads (re-sent context)", p_in_read),
        ("output (what Claude wrote)", p_out),
        ("cache writes (new context)", p_write),
        ("fresh input", p_fresh),
    ):
        frac = value / total.cost if total.cost else 0
        add("    %-30s %s %s  %s" % (
            label, st.cyan(bar(frac)),
            st.dim("%5.1f%%" % (frac * 100)),
            st.bold("$%8s" % format(value, ",.2f")),
        ))
    add("")

    # ---- projects ----------------------------------------------------------
    projects = sorted(a.by_project.items(), key=lambda kv: kv[1].cost, reverse=True)[:top_n]
    if projects:
        add("  " + st.bold("BY PROJECT"))
        top = projects[0][1].cost or 1
        for name, b in projects:
            add("    %-34s %s %s" % (
                squash(name, 34), st.cyan(bar(b.cost / top, 14)),
                st.bold("$%8s" % format(b.cost, ",.2f")),
            ))
        add("")

    # ---- models ------------------------------------------------------------
    add("  " + st.bold("BY MODEL"))
    for model, b in sorted(a.by_model.items(), key=lambda kv: kv[1].cost, reverse=True):
        add("    %-26s %s  %s" % (
            model, st.dim("%5d calls" % b.responses),
            st.bold("$%8s" % format(b.cost, ",.2f")),
        ))
    add("")

    # ---- sessions ----------------------------------------------------------
    sessions = sorted(a.sessions.values(), key=lambda s: s.bucket.cost, reverse=True)[:top_n]
    if sessions:
        add("  " + st.bold("MOST EXPENSIVE SESSIONS"))
        for s in sessions:
            if s.bucket.cost <= 0:
                continue
            flags = []
            if s.tool_errors:
                flags.append(st.red("%d err" % s.tool_errors))
            if s.rework:
                flags.append(st.yellow("%d retry" % s.rework))
            tail = ("  " + st.dim("/ ") + " ".join(flags)) if flags else ""
            add("    %s  %-42s%s" % (
                st.bold("$%7s" % format(s.bucket.cost, ",.2f")), squash(s.label(), 42), tail,
            ))
        add("")

    # ---- tools -------------------------------------------------------------
    if a.tools:
        add("  " + st.bold("TOOL USE") + st.dim("   %d calls, %d failed" % (
            sum(a.tools.values()), sum(a.tool_errors.values()))))
        top = a.tools.most_common(1)[0][1] or 1
        for name, count in a.tools.most_common(top_n):
            errs = a.tool_errors.get(name, 0)
            rate = st.red("  %d failed" % errs) if errs else ""
            add("    %-34s %s %s%s" % (
                squash(name, 34), st.cyan(bar(count / top, 14)),
                st.dim("%5d" % count), rate,
            ))
        add("")

    # ---- cost curve --------------------------------------------------------
    curve = cost_curve(a)
    if len(curve) >= 3:
        add("  " + st.bold("COST OF ONE RESPONSE, BY POSITION IN SESSION"))
        add("    " + st.dim("%-12s %-22s %10s %10s" % ("turn", "", "context", "cost")))
        peak = max(r["avg_cost_usd"] for r in curve) or 1
        for r in curve:
            add("    %-12s %s %10s %10s" % (
                "%d-%d" % (r["turn_start"], r["turn_end"]),
                st.cyan(bar(r["avg_cost_usd"] / peak, 22)),
                st.dim(human(r["avg_context_tokens"])),
                st.bold("$%.3f" % r["avg_cost_usd"]),
            ))
        add("")

    # ---- findings ----------------------------------------------------------
    found = findings(a)
    if found:
        add("  " + st.bold("WHAT TO FIX"))
        for i, (title, body) in enumerate(found, 1):
            add("    %s %s" % (st.yellow("%d." % i), st.bold(title)))
            for chunk in wrap(body, 66):
                add("       " + st.dim(chunk))
            add("")

    add("  " + st.dim("ccxray v%s — nothing left your machine." % __version__))
    add("")
    return "\n".join(L)


def wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------

def render_json(a):
    sessions = sorted(a.sessions.values(), key=lambda s: s.bucket.cost, reverse=True)
    return json.dumps({
        "version": __version__,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "totals": a.total.as_dict(),
        "by_project": {k: v.as_dict() for k, v in a.by_project.items()},
        "by_model": {k: v.as_dict() for k, v in a.by_model.items()},
        "by_day": {k: v.as_dict() for k, v in sorted(a.by_day.items())},
        "sessions": [
            {
                "id": s.sid,
                "project": s.project,
                "label": s.label(),
                "started": s.first_ts,
                "ended": s.last_ts,
                "duration_minutes": round(s.duration_min, 1),
                "user_turns": s.user_turns,
                "tool_calls": s.tool_calls,
                "tool_errors": s.tool_errors,
                "repeated_attempts": s.rework,
                **s.bucket.as_dict(),
            }
            for s in sessions if s.bucket.responses
        ],
        "cost_curve": cost_curve(a),
        "tools": dict(a.tools.most_common()),
        "tool_errors": dict(a.tool_errors.most_common()),
        "top_repeated_commands": dict(a.rework_commands.most_common(15)),
        "most_edited_files": dict(a.files_touched.most_common(15)),
        "findings": [{"title": t, "detail": d} for t, d in findings(a)],
        "scan": {
            "files": a.files_read,
            "lines": a.lines_read,
            "parse_failures": a.parse_failures,
            "duplicate_responses_skipped": a.duplicate_responses,
            "duplicate_cost_avoided_usd": round(a.duplicate_cost, 2),
        },
    }, indent=2)


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

_CSS = """
:root{color-scheme:light;
  --surface-0:#f4f4f2;--surface-1:#fcfcfb;--line:#e4e3df;
  --text-1:#0b0b0b;--text-2:#52514e;--text-3:#84837d;
  --accent:#2a78d6;--accent-soft:#cde2fb;--danger:#e34948;--warn:#eda100;--good:#008300;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
  --surface-0:#121211;--surface-1:#1a1a19;--line:#2e2e2b;
  --text-1:#ffffff;--text-2:#c3c2b7;--text-3:#8d8c84;
  --accent:#3987e5;--accent-soft:#184f95;--danger:#e66767;--warn:#c98500;--good:#008300;}}
:root[data-theme="dark"]{color-scheme:dark;
  --surface-0:#121211;--surface-1:#1a1a19;--line:#2e2e2b;
  --text-1:#ffffff;--text-2:#c3c2b7;--text-3:#8d8c84;
  --accent:#3987e5;--accent-soft:#184f95;--danger:#e66767;--warn:#c98500;--good:#008300;}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-0);color:var(--text-1);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:900px;margin:0 auto;padding:48px 16px 80px}
h1{font-size:19px;letter-spacing:-.01em;margin:0}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--text-3);
  margin:0 0 14px;font-weight:600}
.sub{color:var(--text-3);font-size:13px;margin:4px 0 0}
.card{background:var(--surface-1);border:1px solid var(--line);border-radius:12px;
  padding:22px 24px;margin:0 0 20px}
.hero{display:flex;flex-wrap:wrap;gap:28px;align-items:baseline}
.big{font-size:52px;font-weight:650;letter-spacing:-.03em;line-height:1;
  font-variant-numeric:tabular-nums}
.kv{font-size:13px;color:var(--text-2)}
.kv b{display:block;font-size:19px;color:var(--text-1);font-weight:600;
  font-variant-numeric:tabular-nums}
.row{display:grid;grid-template-columns:1fr 132px 92px;gap:12px;align-items:center;
  padding:5px 0;font-size:13.5px}
.row .nm{color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .val{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.track{height:9px;background:var(--surface-0);border-radius:5px;overflow:hidden}
.fill{height:100%;background:var(--accent);border-radius:0 5px 5px 0}
.spark{width:100%;height:92px;display:block}
.find{border-left:3px solid var(--warn);padding:2px 0 2px 14px;margin:0 0 18px}
.find h3{margin:0 0 4px;font-size:14.5px;font-weight:650}
.find p{margin:0;color:var(--text-2);font-size:13.5px}
.err{color:var(--danger);font-variant-numeric:tabular-nums}
.foot{color:var(--text-3);font-size:12px;text-align:center;margin-top:34px}
a{color:var(--accent)}
@media(max-width:560px){.row{grid-template-columns:1fr 70px}.track{display:none}
  .big{font-size:40px}.wrap{padding:28px 16px 60px}}
"""


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _bars(rows, total):
    """rows: list of (label, value, note_html)"""
    out = []
    peak = max([v for _, v, _ in rows] or [1]) or 1
    for label, value, note in rows:
        pct = max(0.0, min(1.0, value / peak))
        out.append(
            '<div class="row"><div class="nm">%s%s</div>'
            '<div class="track"><div class="fill" style="width:%.1f%%"></div></div>'
            '<div class="val">%s</div></div>'
            % (esc(label), note, pct * 100, esc(total(value)))
        )
    return "".join(out)


def _sparkline(by_day):
    days = sorted(by_day)
    if len(days) < 2:
        return ""
    vals = [by_day[d].cost for d in days]
    peak = max(vals) or 1
    w, h, pad = 860.0, 92.0, 8.0
    step = w / (len(vals) - 1)
    pts = [(i * step, h - pad - (v / peak) * (h - 2 * pad)) for i, v in enumerate(vals)]
    line = " ".join("%.1f,%.1f" % p for p in pts)
    area = "%.1f,%.1f " % (0, h) + line + " %.1f,%.1f" % (w, h)
    return (
        '<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none" '
        'role="img" aria-label="Daily spend from %s to %s">'
        '<polygon points="%s" fill="var(--accent-soft)" opacity=".55"/>'
        '<polyline points="%s" fill="none" stroke="var(--accent)" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/></svg>'
        '<div class="row" style="grid-template-columns:1fr 1fr;color:var(--text-3);'
        'font-size:12px"><div>%s</div><div style="text-align:right">%s</div></div>'
        % (int(w), int(h), esc(days[0]), esc(days[-1]), area, line, esc(days[0]), esc(days[-1]))
    )


def anonymize(a):
    """Strip identifying names so a report can be shared. Numbers are untouched."""
    projects = {}
    renamed = defaultdict(Bucket)
    for i, (name, bucket) in enumerate(
            sorted(a.by_project.items(), key=lambda kv: kv[1].cost, reverse=True), 1):
        alias = "project-%d" % i
        projects[name] = alias
        renamed[alias] = bucket
    a.by_project = renamed

    for i, s in enumerate(
            sorted(a.sessions.values(), key=lambda s: s.bucket.cost, reverse=True), 1):
        s.title = "session-%d" % i
        s.first_prompt = None
        s.project = projects.get(s.project, s.project)

    # MCP tool names embed the server name (mcp__<server>__<tool>), which
    # says what the user works on. Built-in tool names reveal nothing, so keep
    # them - they are the useful part of the report.
    servers, tools = {}, {}
    for name, _ in a.tools.most_common():          # stable, frequency-ordered
        if not name.startswith("mcp__"):
            continue
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "?"
        servers.setdefault(server, "mcp-server-%d" % (len(servers) + 1))
        tools.setdefault(name, "%s__tool-%d" % (servers[server], len(tools) + 1))

    def mask(name):
        return tools.get(name, name if not name.startswith("mcp__") else "mcp-server-x__tool-x")

    for counter_name in ("tools", "tool_errors"):
        masked = Counter()
        for name, count in getattr(a, counter_name).items():
            masked[mask(name)] += count
        setattr(a, counter_name, masked)

    a.files_touched = Counter(
        {"file-%d" % i: c for i, (_, c) in enumerate(a.files_touched.most_common(), 1)})
    a.rework_commands = Counter(
        {"command-%d" % i: c for i, (_, c) in enumerate(a.rework_commands.most_common(), 1)})
    a.error_messages = Counter()
    return a


def render_html(a):
    money = lambda v: "$" + format(v, ",.2f")
    count = lambda v: format(int(v), ",")
    total = a.total
    days = sorted(a.by_day)

    p_read = p_write = 0.0
    for model, b in a.by_model.items():
        p_in, _ = price_for(model)
        p_read += b.cache_read * p_in * CACHE_READ_MULTIPLIER[model] / 1e6
        p_write += (b.c5m * CACHE_WRITE_5M + b.c1h * CACHE_WRITE_1H) * p_in / 1e6
    p_out = sum(b.out * price_for(m)[1] for m, b in a.by_model.items()) / 1e6
    p_fresh = sum(b.inp * price_for(m)[0] for m, b in a.by_model.items()) / 1e6

    parts = []
    A = parts.append
    A('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width,initial-scale=1">')
    A("<title>ccxray</title><style>%s</style></head><body><div class='wrap'>" % _CSS)

    A("<h1>ccxray</h1><p class='sub'>What your Claude Code sessions actually cost. "
      "Generated %s &middot; nothing left your machine.</p>"
      % esc(_dt.datetime.now().strftime("%d %b %Y, %H:%M")))

    if total.responses == 0:
        A("<div class='card'>No usage data found.</div></div></body></html>")
        return "".join(parts)

    A("<div class='card' style='margin-top:20px'><div class='hero'>")
    A("<div><div class='big'>%s</div><div class='kv' style='margin-top:6px'>"
      "at list API prices</div></div>" % esc(money(total.cost)))
    for label, value in (("Sessions", count(len(a.sessions))),
                         ("Tokens", human(total.total_tokens)),
                         ("Tool calls", count(sum(a.tools.values()))),
                         ("Failed", count(sum(a.tool_errors.values())))):
        A("<div class='kv'><b>%s</b>%s</div>" % (esc(value), esc(label)))
    A("</div></div>")

    if len(days) > 1:
        A("<div class='card'><h2>Daily spend</h2>%s</div>" % _sparkline(a.by_day))

    A("<div class='card'><h2>Where the money goes</h2>%s</div>" % _bars([
        ("Cache reads — re-sent context", p_read, ""),
        ("Output — what Claude wrote", p_out, ""),
        ("Cache writes — new context", p_write, ""),
        ("Fresh input", p_fresh, ""),
    ], money))

    projects = sorted(a.by_project.items(), key=lambda kv: kv[1].cost, reverse=True)[:10]
    A("<div class='card'><h2>By project</h2>%s</div>"
      % _bars([(k, v.cost, "") for k, v in projects], money))

    sessions = [s for s in sorted(a.sessions.values(), key=lambda s: s.bucket.cost,
                                  reverse=True)[:10] if s.bucket.cost > 0]
    rows = []
    for s in sessions:
        note = ""
        if s.tool_errors:
            note = " <span class='err'>&middot; %d failed</span>" % s.tool_errors
        rows.append((s.label(), s.bucket.cost, note))
    A("<div class='card'><h2>Most expensive sessions</h2>%s</div>" % _bars(rows, money))

    trows = []
    for name, c in a.tools.most_common(10):
        errs = a.tool_errors.get(name, 0)
        note = " <span class='err'>&middot; %d failed</span>" % errs if errs else ""
        trows.append((name, c, note))
    A("<div class='card'><h2>Tool use</h2>%s</div>" % _bars(trows, count))

    curve = cost_curve(a)
    if len(curve) >= 3:
        rows = [("turn %d–%d  ·  %s ctx" % (r["turn_start"], r["turn_end"],
                                                  human(r["avg_context_tokens"])),
                 r["avg_cost_usd"], "") for r in curve]
        A("<div class='card'><h2>Cost of one response, by position in session</h2>%s"
          "<p class='sub' style='margin-top:12px'>Each turn re-reads everything before "
          "it, so the same work costs more the later it happens.</p></div>"
          % _bars(rows, lambda v: "$%.3f" % v))

    found = findings(a)
    if found:
        A("<div class='card'><h2>What to fix</h2>")
        for title, body in found:
            A("<div class='find'><h3>%s</h3><p>%s</p></div>" % (esc(title), esc(body)))
        A("</div>")

    A("<p class='foot'>ccxray v%s &middot; scanned %s records across %d files</p>"
      % (__version__, count(a.lines_read), a.files_read))
    A("</div></body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def find_transcripts(root):
    root = os.path.expanduser(root)
    if os.path.isfile(root):
        return [root]
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ccxray",
        description="X-ray your Claude Code sessions: true cost, cache waste, and rework.",
    )
    ap.add_argument("--dir", default="~/.claude/projects",
                    help="transcript directory (default: ~/.claude/projects)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--html", metavar="FILE", help="write a shareable HTML report")
    ap.add_argument("--top", type=int, default=8, help="rows per table (default: 8)")
    ap.add_argument("--anonymize", action="store_true",
                    help="replace project, session and file names with placeholders "
                         "so the report is safe to share")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    ap.add_argument("--version", action="version", version="ccxray " + __version__)
    args = ap.parse_args(argv)

    paths = find_transcripts(args.dir)
    if not paths:
        sys.stderr.write(
            "No Claude Code transcripts found in %s\n"
            "Point --dir at the folder that holds your .jsonl session files.\n"
            % os.path.expanduser(args.dir)
        )
        return 1

    a = scan(paths)
    if args.anonymize:
        a = anonymize(a)

    if args.json:
        print(render_json(a))
        return 0

    if args.html:
        path = os.path.expanduser(args.html)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_html(a))
        print("Wrote %s" % path)
        return 0

    color = sys.stdout.isatty() and not args.no_color and os.environ.get("TERM") != "dumb"
    print(render_terminal(a, Style(color), top_n=args.top))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
