"""Offline tests for routing_eval.py. Every classifier call is faked; nothing
here touches the API.

    python3 -m unittest discover -s evals -p 'test_*.py' -v
"""
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routing_eval as ev  # noqa: E402


def fake_body(text, model_id="claude-haiku-5-5"):
    return json.dumps({"result": text, "total_cost_usd": 0.0001,
                       "usage": {"input_tokens": 400, "output_tokens": 40},
                       "modelUsage": {model_id: {}}})


class NoSubprocess(unittest.TestCase):
    """Fail loudly if anything tries to spawn the real CLI."""

    def setUp(self):
        p = mock.patch.object(ev.subprocess, "run", side_effect=AssertionError("real subprocess call"))
        p.start()
        self.addCleanup(p.stop)


class ParseAndGrade(unittest.TestCase):
    def test_parse_reply(self):
        self.assertEqual(ev.parse_reply('sure {"model":"opus","effort":"high","reason":"x"}')["model"], "opus")
        self.assertIsNone(ev.parse_reply('{"model":"gpt"}')["model"])
        self.assertIsNone(ev.parse_reply("no json")["model"])
        self.assertIsNone(ev.parse_reply("{not json}")["model"])
        self.assertIsNone(ev.parse_reply("[1]")["model"])

    def test_grade(self):
        self.assertEqual(ev.grade("sonnet", ["sonnet", "opus"]), "ok")
        self.assertEqual(ev.grade("haiku", ["sonnet", "opus"]), "under")
        self.assertEqual(ev.grade("opus", ["haiku", "sonnet"]), "over")
        self.assertEqual(ev.grade("haiku", ["opus", "sonnet"]), "under")
        self.assertEqual(ev.grade(None, ["haiku"]), "failed")


class Classify(unittest.TestCase):
    def test_records_raw_parsed_and_model_ids(self):
        proc = subprocess.CompletedProcess([], 0, fake_body('{"model":"sonnet","effort":"medium","reason":"r"}'), "")
        with mock.patch.object(ev.subprocess, "run", return_value=proc) as run:
            out = ev.classify("SYS", {"prompt": "hi"})
        self.assertEqual(out["parsed"]["model"], "sonnet")
        self.assertIn('"sonnet"', out["raw"])
        self.assertEqual(out["model_ids"], ["claude-haiku-5-5"])
        self.assertIsNone(out["error"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "SYS")

    def test_error_is_recorded_not_raised(self):
        proc = subprocess.CompletedProcess([], 1, "not json", "")
        with mock.patch.object(ev.subprocess, "run", return_value=proc):
            out = ev.classify("SYS", {"prompt": "hi"})
        self.assertIsNone(out["parsed"]["model"])
        self.assertTrue(out["error"])


class ResultsPath(unittest.TestCase):
    def test_suffix_on_same_day(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertEqual(ev.results_path("2026-10-14", d).name, "2026-10-14.json")
            (d / "2026-10-14.json").write_text("{}")
            self.assertEqual(ev.results_path("2026-10-14", d).name, "2026-10-14-2.json")
            (d / "2026-10-14-2.json").write_text("{}")
            self.assertEqual(ev.results_path("2026-10-14", d).name, "2026-10-14-3.json")


class Cases(unittest.TestCase):
    def test_case_files_are_valid_and_ids_unique(self):
        cases = ev.load_cases()
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(any(c["set"] == "heldout" for c in cases))
        for c in cases:
            self.assertTrue(c["prompt"])
            self.assertTrue(c["expect"])
            self.assertTrue(set(c["expect"]) <= set(ev.TIERS), c["id"])


class EndToEndMocked(NoSubprocess):
    def run_main(self, argv, picks):
        """picks: case id -> list of tiers to return on successive runs."""
        calls = {}

        def fake(system, case):
            i = calls.get(case["id"], 0)
            calls[case["id"]] = i + 1
            tier = picks[case["id"]][i]
            raw = json.dumps({"model": tier, "effort": "low", "reason": "fake"}) if tier else "garbage"
            return {"raw": raw, "parsed": ev.parse_reply(raw), "error": None,
                    "model_ids": ["claude-haiku-5-5"], "usage": None, "cost": 0.001}

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ev, "claude_version", return_value="test"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            path = ev.main(argv + ["--out-dir", d, "--workers", "1"], classify_fn=fake)
            data = json.loads(path.read_text())
            return data, out.getvalue(), path

    def test_results_file_and_split_accuracy(self):
        cases = {c["id"]: c for c in ev.load_cases()}
        orig_id = next(i for i, c in cases.items() if c["set"] == "original")
        ho_id = next(i for i, c in cases.items() if c["set"] == "heldout")
        right = lambda i: cases[i]["expect"][0]
        wrong = lambda i: next(t for t in ev.TIERS if t not in cases[i]["expect"])
        picks = {orig_id: [right(orig_id)] * 2, ho_id: [wrong(ho_id), None]}
        data, out, path = self.run_main(["--runs", "2", "--only", f"{orig_id},{ho_id}"], picks)

        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}\.json$")
        self.assertEqual(data["runs_per_case"], 2)
        self.assertEqual(data["models"]["classifier_requested"], "haiku")
        self.assertEqual(data["models"]["classifier_resolved"], ["claude-haiku-5-5"])
        self.assertEqual(data["router_prompt_sha256"],
                         hashlib.sha256(ev.plugin_system_prompt().encode()).hexdigest())
        self.assertIn("timestamp", data)

        s = data["summary"]
        self.assertEqual((s["original"]["ok"], s["original"]["classifications"]), (2, 2))
        self.assertEqual(s["original"]["accuracy"], 1.0)
        self.assertEqual(s["heldout"]["ok"], 0)
        self.assertEqual(s["heldout"]["failed"], 1)
        self.assertEqual(s["overall"]["accuracy"], 0.5)
        self.assertEqual(s["original"]["stable_cases"], 1)
        self.assertEqual(s["heldout"]["stable_cases"], 0)

        rows = {r["id"]: r for r in data["cases"]}
        self.assertTrue(rows[orig_id]["passed"])
        self.assertFalse(rows[ho_id]["passed"])
        self.assertEqual(rows[ho_id]["expect"], cases[ho_id]["expect"])
        self.assertIn("<new_message>", rows[ho_id]["input"])
        self.assertEqual(len(rows[ho_id]["runs"]), 2)
        self.assertEqual(rows[ho_id]["runs"][1]["raw"], "garbage")
        self.assertEqual(rows[ho_id]["runs"][1]["grade"], "failed")

        for label in ("original:", "heldout:", "overall:"):
            self.assertIn(label, out)


if __name__ == "__main__":
    unittest.main()
