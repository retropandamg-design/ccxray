#!/usr/bin/env python3
"""
Generate a synthetic set of Claude Code transcripts.

Useful for trying ccxray without pointing it at your own sessions, and for
producing screenshots that contain nobody's real data.

    python3 make_sample.py sample/
    python3 ccxray.py --dir sample/
"""

import json
import os
import random
import sys
import datetime as dt

PROJECTS = [
    ("/home/dev/work/checkout-service", 9),
    ("/home/dev/work/design-system", 6),
    ("/home/dev/work/etl-pipeline", 5),
    ("/home/dev/work/docs-site", 3),
    ("/home/dev/scratch/spike", 2),
]
TOPICS = [
    "Fix flaky checkout test", "Migrate auth middleware", "Add retry to webhook sender",
    "Refactor pricing table", "Investigate slow query", "Port build to CI",
    "Write integration tests", "Upgrade dependency", "Debug memory growth",
    "Clean up dead config", "Add pagination", "Fix timezone handling",
]
TOOLS = [("Bash", 46), ("Read", 18), ("Edit", 14), ("Grep", 9),
         ("Write", 6), ("Glob", 4), ("WebFetch", 3)]
BASE_CMDS = ["npm test", "npm run build", "git status", "pytest -q", "make lint",
             "ls -la", "git diff --stat", "npm run typecheck", "git log --oneline -5",
             "cat package.json", "df -h", "node scripts/seed.js", "make clean"]


def a_command(rng):
    """Mostly unique commands, with the occasional genuine retry."""
    if rng.random() < 0.08:                       # a real repeat, as happens
        return rng.choice(BASE_CMDS)
    return "%s %s" % (rng.choice(BASE_CMDS), rng.choice(
        ["", "--verbose", "-q", "2>&1 | tail -20", "--no-cache",
         "-- tests/unit", "--watch=false", "| head", "--force"])) \
        + (" # %d" % rng.randint(1, 9999) if rng.random() < .5 else "")


def weighted(pairs, rng):
    total = sum(w for _, w in pairs)
    pick = rng.uniform(0, total)
    upto = 0
    for value, weight in pairs:
        upto += weight
        if pick <= upto:
            return value
    return pairs[-1][0]


def build(out_dir, seed=7):
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    start = dt.datetime(2026, 4, 6, 9, 0, 0)
    counter = [0]

    for cwd, n_sessions in PROJECTS:
        folder = os.path.join(out_dir, "-" + cwd.strip("/").replace("/", "-"))
        os.makedirs(folder, exist_ok=True)

        for _ in range(n_sessions):
            sid = "%08x-0000-4000-8000-%012x" % (rng.getrandbits(32), rng.getrandbits(48))
            when = start + dt.timedelta(days=rng.randint(0, 13), hours=rng.randint(0, 9))
            # A few long sessions, most short - this is what real usage looks like.
            turns = rng.choice([12, 18, 25, 40, 60] + ([140, 260, 380] if rng.random() < .3 else []))
            context = rng.randint(14000, 26000)
            lines = [json.dumps({"type": "custom-title", "sessionId": sid,
                                 "customTitle": rng.choice(TOPICS)})]

            for turn in range(turns):
                when += dt.timedelta(seconds=rng.randint(20, 150))
                stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
                counter[0] += 1
                mid = "msg_%016x" % counter[0]

                tool = weighted(TOOLS, rng)
                tool_input = ({"command": a_command(rng)} if tool == "Bash"
                              else {"file_path": "%s/src/mod_%d.py" % (cwd, rng.randint(1, 9))})
                tool_id = "toolu_%012x" % counter[0]

                # Context grows with every turn - the whole point of the report.
                context += rng.randint(900, 2600)
                out_tokens = rng.randint(220, 1500)
                model = "claude-sonnet-5" if rng.random() < 0.08 else "claude-opus-5"

                lines.append(json.dumps({
                    "type": "assistant", "timestamp": stamp, "cwd": cwd, "sessionId": sid,
                    "message": {
                        "id": mid, "model": model, "role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id,
                                     "name": tool, "input": tool_input}],
                        "usage": {
                            "input_tokens": 2, "output_tokens": out_tokens,
                            "cache_read_input_tokens": context,
                            "cache_creation": {"ephemeral_1h_input_tokens": rng.randint(1200, 5200),
                                               "ephemeral_5m_input_tokens": 0},
                            "output_tokens_details": {"thinking_tokens": out_tokens // 3},
                        },
                    },
                }))

                failed = rng.random() < 0.055
                lines.append(json.dumps({
                    "type": "user", "timestamp": stamp, "cwd": cwd, "sessionId": sid,
                    "message": {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool_id, "is_error": failed,
                        "content": "Error: command failed with exit code 1" if failed else "ok"}]},
                }))

            with open(os.path.join(folder, sid + ".jsonl"), "w") as fh:
                fh.write("\n".join(lines) + "\n")

    return sum(n for _, n in PROJECTS)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "sample"
    n = build(target)
    print("Wrote %d synthetic sessions to %s/" % (n, target))
    print("Now run:  python3 ccxray.py --dir %s" % target)
