#!/usr/bin/env python3
"""Tests for ccxray. Run: python3 -m unittest discover -v"""

import json
import os
import shutil
import tempfile
import unittest

import ccxray


def response(mid, model="claude-opus-5", out=0, read=0, c1h=0, c5m=0,
             ts="2026-01-01T10:00:00Z", cwd="/work/proj", tools=()):
    content = [{"type": "tool_use", "id": "t-" + mid, "name": n, "input": i}
               for n, i in tools]
    return json.dumps({
        "type": "assistant", "timestamp": ts, "cwd": cwd,
        "message": {
            "id": mid, "model": model, "role": "assistant", "content": content,
            "usage": {
                "input_tokens": 0, "output_tokens": out,
                "cache_read_input_tokens": read,
                "cache_creation": {"ephemeral_1h_input_tokens": c1h,
                                   "ephemeral_5m_input_tokens": c5m},
            },
        },
    })


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, lines):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def scan(self):
        return ccxray.scan(ccxray.find_transcripts(self.dir))


class TestPricing(Harness):
    def test_output_priced_at_list_rate(self):
        # 1M output tokens on Opus 5 is $25.00
        self.assertAlmostEqual(
            ccxray.cost_of("claude-opus-5", 0, 1_000_000, 0, 0, 0), 25.0, places=6)

    def test_cache_read_is_a_tenth_of_input(self):
        # 1M cache-read tokens at $5/M input x 0.1 = $0.50
        self.assertAlmostEqual(
            ccxray.cost_of("claude-opus-5", 0, 0, 1_000_000, 0, 0), 0.50, places=6)

    def test_one_hour_cache_write_costs_more_than_five_minute(self):
        hour = ccxray.cost_of("claude-opus-5", 0, 0, 0, 0, 1_000_000)
        five = ccxray.cost_of("claude-opus-5", 0, 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(hour, 10.0, places=6)   # 5 * 2.0
        self.assertAlmostEqual(five, 6.25, places=6)   # 5 * 1.25
        self.assertGreater(hour, five)

    def test_unknown_model_falls_back_to_opus_tier(self):
        self.assertEqual(ccxray.price_for("claude-from-the-future-9"),
                         ccxray.DEFAULT_PRICE)

    def test_fable_reads_cache_more_cheaply(self):
        self.assertLess(
            ccxray.cost_of("claude-fable-5-1", 0, 0, 1_000_000, 0, 0)
            / ccxray.price_for("claude-fable-5-1")[0],
            ccxray.cost_of("claude-opus-5", 0, 0, 1_000_000, 0, 0)
            / ccxray.price_for("claude-opus-5")[0])


class TestDeduplication(Harness):
    def test_same_message_id_across_files_counted_once(self):
        # This is the whole point: resuming a session copies turns forward.
        self.write("a.jsonl", [response("m1", out=1_000_000)])
        self.write("b.jsonl", [response("m1", out=1_000_000),
                               response("m2", out=1_000_000)])
        a = self.scan()
        self.assertEqual(a.total.responses, 2)
        self.assertEqual(a.duplicate_responses, 1)
        self.assertAlmostEqual(a.total.cost, 50.0, places=4)
        self.assertAlmostEqual(a.duplicate_cost, 25.0, places=4)

    def test_duplicate_cost_is_reported_not_silently_dropped(self):
        self.write("a.jsonl", [response("m1", out=1_000_000)])
        self.write("b.jsonl", [response("m1", out=1_000_000)])
        self.assertGreater(self.scan().duplicate_cost, 0)


class TestRobustness(Harness):
    def test_malformed_lines_are_counted_and_skipped(self):
        self.write("a.jsonl", ["{not json", "", "[]", "null",
                               response("m1", out=1_000_000)])
        a = self.scan()
        self.assertEqual(a.total.responses, 1)
        self.assertGreaterEqual(a.parse_failures, 1)

    def test_missing_usage_does_not_crash(self):
        self.write("a.jsonl", ['{"type":"assistant","message":{"id":"x"}}',
                               '{"type":"user","message":{"content":"hi"}}'])
        self.assertEqual(self.scan().total.responses, 0)

    def test_synthetic_model_is_ignored(self):
        self.write("a.jsonl", [response("m1", model="<synthetic>", out=1_000_000)])
        self.assertEqual(self.scan().total.responses, 0)

    def test_every_renderer_survives_empty_input(self):
        self.write("a.jsonl", ["{}"])
        a = self.scan()
        self.assertIn("No usage data", ccxray.render_terminal(a, ccxray.Style(False)))
        self.assertIn("No usage data", ccxray.render_html(a))
        json.loads(ccxray.render_json(a))


class TestAnalysis(Harness):
    def test_project_name_comes_from_recorded_cwd(self):
        self.write("a.jsonl", [response("m1", out=10, cwd="/home/me/apps/widget")])
        self.assertIn("apps/widget", self.scan().by_project)

    def test_repeated_bash_command_flagged_as_rework(self):
        cmd = [("Bash", {"command": "make test"})]
        self.write("a.jsonl", [response("m%d" % i, out=1, tools=cmd) for i in range(3)])
        self.assertEqual(sum(s.rework for s in self.scan().sessions.values()), 2)

    def test_distinct_commands_are_not_rework(self):
        self.write("a.jsonl", [
            response("m1", out=1, tools=[("Bash", {"command": "ls"})]),
            response("m2", out=1, tools=[("Bash", {"command": "pwd"})])])
        self.assertEqual(sum(s.rework for s in self.scan().sessions.values()), 0)

    def test_tool_errors_are_attributed_to_the_tool(self):
        self.write("a.jsonl", [
            response("m1", out=1, tools=[("Bash", {"command": "nope"})]),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t-m1",
                 "is_error": True, "content": "boom"}]}})])
        self.assertEqual(self.scan().tool_errors["Bash"], 1)

    def test_cost_curve_rises_with_growing_context(self):
        lines = [response("m%d" % i, out=10, read=1000 * i) for i in range(120)]
        self.write("a.jsonl", lines)
        curve = ccxray.cost_curve(self.scan(), width=25, min_len=40, min_samples=5)
        self.assertGreaterEqual(len(curve), 3)
        self.assertGreater(curve[-1]["avg_cost_usd"], curve[0]["avg_cost_usd"])
        self.assertGreater(curve[-1]["avg_context_tokens"], curve[0]["avg_context_tokens"])


class TestAnonymize(Harness):
    def test_identifying_names_are_replaced(self):
        self.write("a.jsonl", [response("m1", out=10, cwd="/home/me/secret-client")])
        a = ccxray.anonymize(self.scan())
        blob = ccxray.render_json(a) + ccxray.render_html(a)
        self.assertNotIn("secret-client", blob)
        self.assertIn("project-1", blob)

    def test_numbers_are_untouched(self):
        self.write("a.jsonl", [response("m1", out=1_000_000, cwd="/x/y")])
        before = self.scan().total.cost
        self.assertAlmostEqual(ccxray.anonymize(self.scan()).total.cost, before, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
