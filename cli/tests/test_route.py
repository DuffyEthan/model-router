import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import route  # noqa: E402


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def claude_json(structured=None, result="", cost=0.0003):
    body = {"total_cost_usd": cost, "result": result}
    if structured is not None:
        body["structured_output"] = structured
    return json.dumps(body)


class ClassifyTests(unittest.TestCase):
    def run_classify(self, proc):
        with mock.patch.object(route.subprocess, "run", return_value=proc) as run:
            return route.classify("hello"), run

    def test_uses_structured_output(self):
        d, _ = self.run_classify(completed(claude_json({"model": "opus", "effort": "xhigh", "reason": "hard"})))
        self.assertEqual((d["model"], d["effort"]), ("opus", "xhigh"))
        self.assertEqual(d["router_cost_usd"], 0.0003)

    def test_falls_back_to_result_text(self):
        d, _ = self.run_classify(completed(claude_json(result='{"model":"haiku","effort":"low","reason":"x"}')))
        self.assertEqual(d["model"], "haiku")

    def test_invalid_model_falls_back(self):
        d, _ = self.run_classify(completed(claude_json({"model": "gpt", "effort": "low", "reason": "x"})))
        self.assertEqual(d["model"], route.FALLBACK["model"])

    def test_non_json_output_falls_back(self):
        d, _ = self.run_classify(completed("not json"))
        self.assertEqual(d, route.FALLBACK)

    def test_timeout_falls_back(self):
        with mock.patch.object(route.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 60)):
            self.assertEqual(route.classify("hello"), route.FALLBACK)

    def test_missing_claude_cli_falls_back(self):
        with mock.patch.object(route.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(route.classify("hello"), route.FALLBACK)

    def test_classifier_is_cheap_and_isolated(self):
        _, run = self.run_classify(completed(claude_json({"model": "haiku", "effort": "low", "reason": "x"})))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")  # no tools in context
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--strict-mcp-config", cmd)

    def test_classifier_gets_plugin_prompt_and_framing(self):
        _, run = self.run_classify(completed(claude_json({"model": "haiku", "effort": "low", "reason": "x"})))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], route.SYSTEM_PROMPT)
        self.assertEqual(cmd[-1], "<new_message>\nhello\n</new_message>")


class MainTests(unittest.TestCase):
    def run_main(self, argv, decision=None, stdin=None):
        decision = decision or {"model": "sonnet", "effort": "medium", "reason": "coding"}
        with mock.patch.object(sys, "argv", ["route", *argv]), \
             mock.patch.object(route, "classify", return_value=dict(decision)) as classify, \
             mock.patch.object(route, "log") as log, \
             mock.patch.object(route.os, "execvp") as execvp, \
             mock.patch.object(sys, "stdin", stdin or io.StringIO()), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            route.main()
        return classify, log, execvp

    def test_dry_run_does_not_launch_claude(self):
        _, log, execvp = self.run_main(["--dry-run", "do a thing"])
        execvp.assert_not_called()
        log.assert_called_once()

    def test_launches_claude_with_routed_model_and_effort(self):
        _, _, execvp = self.run_main(["do a thing"])
        cmd = execvp.call_args.args[1]
        self.assertEqual(cmd[:5], ["claude", "--model", "sonnet", "--effort", "medium"])
        self.assertEqual(cmd[-1], "do a thing")
        self.assertNotIn("-p", cmd)

    def test_print_flag_runs_headless(self):
        _, _, execvp = self.run_main(["-p", "do a thing"])
        self.assertIn("-p", execvp.call_args.args[1])

    def test_force_model_skips_classification(self):
        classify, _, execvp = self.run_main(["--model", "opus", "do a thing"])
        classify.assert_not_called()
        self.assertEqual(execvp.call_args.args[1][2], "opus")

    def test_effort_override(self):
        _, _, execvp = self.run_main(["--effort", "high", "do a thing"])
        self.assertEqual(execvp.call_args.args[1][4], "high")

    def test_args_after_double_dash_pass_through_to_claude(self):
        _, _, execvp = self.run_main(["do a thing", "--", "--permission-mode", "plan"])
        cmd = execvp.call_args.args[1]
        self.assertEqual(cmd[5:7], ["--permission-mode", "plan"])
        self.assertEqual(cmd[-1], "do a thing")

    def test_unknown_equals_flags_pass_through(self):
        _, _, execvp = self.run_main(["--permission-mode=plan", "do a thing"])
        cmd = execvp.call_args.args[1]
        self.assertIn("--permission-mode=plan", cmd)
        self.assertEqual(cmd[-1], "do a thing")

    def test_prompt_from_stdin(self):
        stdin = io.StringIO("piped prompt\n")
        stdin.isatty = lambda: False
        classify, _, execvp = self.run_main(["-p"], stdin=stdin)
        classify.assert_called_once_with("piped prompt")
        self.assertEqual(execvp.call_args.args[1][-1], "piped prompt")

    def test_no_prompt_is_an_error(self):
        with mock.patch.object(sys, "argv", ["route"]), \
             mock.patch.object(sys, "stdin", io.StringIO()), \
             mock.patch("sys.stderr", new_callable=io.StringIO), \
             self.assertRaises(SystemExit):
            route.main()


class LogTests(unittest.TestCase):
    def test_log_appends_jsonl_and_truncates_prompt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "log.jsonl"
            with mock.patch.object(route, "LOG_PATH", path):
                route.log("x" * 500, {"model": "haiku", "effort": "low", "reason": "r"})
            row = json.loads(path.read_text())
            self.assertEqual(len(row["prompt"]), 200)
            self.assertEqual(row["model"], "haiku")

    def test_log_failure_is_silent(self):
        with mock.patch.object(route, "LOG_PATH", Path("/nonexistent-root/x/log.jsonl")):
            route.log("p", {"model": "haiku"})  # must not raise


if __name__ == "__main__":
    unittest.main()
