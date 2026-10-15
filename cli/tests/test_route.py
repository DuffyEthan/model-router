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
        with mock.patch.object(route.subprocess, "run", return_value=proc) as run, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            return route.classify("hello"), run

    def assert_session_default(self, **run_kwargs):
        with mock.patch.object(route.subprocess, "run", **run_kwargs), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            d = route.classify("hello")
        self.assertEqual(d, route.SESSION_DEFAULT)
        self.assertIsNone(d["model"])
        self.assertIsNone(d["effort"])
        self.assertIn("using the session model", err.getvalue())

    def test_uses_structured_output(self):
        d, _ = self.run_classify(completed(claude_json({"model": "opus", "effort": "xhigh", "reason": "hard"})))
        self.assertEqual((d["model"], d["effort"]), ("opus", "xhigh"))
        self.assertEqual(d["router_cost_usd"], 0.0003)

    def test_falls_back_to_result_text(self):
        d, _ = self.run_classify(completed(claude_json(result='{"model":"haiku","effort":"low","reason":"x"}')))
        self.assertEqual(d["model"], "haiku")

    def test_unknown_model_uses_session_default(self):
        # The plugin leaves the session model alone when the classifier names an unknown model.
        self.assert_session_default(
            return_value=completed(claude_json({"model": "gpt", "effort": "low", "reason": "x"})))

    def test_missing_model_uses_session_default(self):
        self.assert_session_default(return_value=completed(claude_json({"effort": "low", "reason": "x"})))

    def test_non_object_reply_uses_session_default(self):
        self.assert_session_default(return_value=completed(claude_json(result='["opus"]')))

    def test_non_json_output_uses_session_default(self):
        self.assert_session_default(return_value=completed("not json"))

    def test_unparseable_result_text_uses_session_default(self):
        self.assert_session_default(return_value=completed(claude_json(result="I think opus")))

    def test_timeout_uses_session_default(self):
        self.assert_session_default(side_effect=subprocess.TimeoutExpired("claude", 60))

    def test_missing_claude_cli_uses_session_default(self):
        self.assert_session_default(side_effect=FileNotFoundError)

    def test_unknown_effort_keeps_model_at_medium(self):
        # Mirrors the plugin: a valid model with a bad effort is kept, effort becomes medium.
        d, _ = self.run_classify(completed(claude_json({"model": "opus", "effort": "max", "reason": "x"})))
        self.assertEqual((d["model"], d["effort"]), ("opus", "medium"))

    def test_missing_reason_is_tolerated(self):
        d, _ = self.run_classify(completed(claude_json({"model": "haiku", "effort": "low"})))
        self.assertEqual((d["model"], d["reason"]), ("haiku", ""))

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
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            route.main()
        self.stderr = err.getvalue()
        return classify, log, execvp

    def test_classifier_failure_launches_with_session_model(self):
        _, _, execvp = self.run_main(["do a thing", "--", "--permission-mode", "plan"],
                                     decision=route.SESSION_DEFAULT)
        cmd = execvp.call_args.args[1]
        self.assertEqual(cmd, ["claude", "--permission-mode", "plan", "do a thing"])
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--effort", cmd)
        self.assertIn("session model", self.stderr)

    def test_classifier_failure_keeps_explicit_effort(self):
        # An effort the user typed is their choice, not the router's, so it survives a failure.
        _, _, execvp = self.run_main(["--effort", "high", "do a thing"], decision=route.SESSION_DEFAULT)
        self.assertEqual(execvp.call_args.args[1], ["claude", "--effort", "high", "do a thing"])

    def test_classifier_timeout_end_to_end(self):
        with mock.patch.object(sys, "argv", ["route", "do a thing"]), \
             mock.patch.object(route.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 60)), \
             mock.patch.object(route, "log"), \
             mock.patch.object(route.os, "execvp") as execvp, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            route.main()
        self.assertEqual(execvp.call_args.args[1], ["claude", "do a thing"])
        self.assertIn("classifier failed", err.getvalue())

    def test_classifier_failure_dry_run(self):
        _, _, execvp = self.run_main(["--dry-run", "do a thing"], decision=route.SESSION_DEFAULT)
        execvp.assert_not_called()
        self.assertIn("[router] → session model: classifier failed", self.stderr)

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


class ValidationTests(unittest.TestCase):
    def run_invalid(self, argv, decision=None):
        """Run main() expecting a validation error; return (exit code, stderr, mocks)."""
        decision = decision or {"model": "sonnet", "effort": "medium", "reason": "coding"}
        with mock.patch.object(sys, "argv", ["route", *argv]), \
             mock.patch.object(route, "classify", return_value=dict(decision)) as classify, \
             mock.patch.object(route, "log"), \
             mock.patch.object(route.os, "execvp") as execvp, \
             mock.patch.object(sys, "stdin", io.StringIO()), \
             mock.patch("sys.stderr", new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as cm:
            route.main()
        execvp.assert_not_called()
        self.assertNotIn(cm.exception.code, (0, None))
        return str(cm.exception.code), classify

    def test_invalid_model_flag(self):
        msg, classify = self.run_invalid(["--model", "gpt-4", "do a thing"])
        self.assertIn("invalid model 'gpt-4'", msg)
        self.assertIn("haiku, sonnet, opus", msg)
        classify.assert_not_called()

    def test_empty_model_flag_is_rejected_not_ignored(self):
        msg, classify = self.run_invalid(["--model", "", "do a thing"])
        self.assertIn("invalid model ''", msg)
        classify.assert_not_called()

    def test_invalid_effort_flag(self):
        msg, classify = self.run_invalid(["--effort", "max", "do a thing"])
        self.assertIn("invalid effort 'max'", msg)
        self.assertIn("low, medium, high, xhigh", msg)
        classify.assert_not_called()  # no classifier call wasted on a bad flag

    def test_invalid_effort_with_forced_model(self):
        msg, _ = self.run_invalid(["--model", "opus", "--effort", "HIGH", "do a thing"])
        self.assertIn("invalid effort 'HIGH'", msg)

    def test_invalid_flags_rejected_on_dry_run_too(self):
        msg, _ = self.run_invalid(["--dry-run", "--model", "o", "do a thing"])
        self.assertIn("invalid model 'o'", msg)

    def test_build_command_refuses_unknown_model(self):
        # Belt and braces: even a decision that skipped classify()'s checks can't reach exec.
        msg, _ = self.run_invalid(["do a thing"], decision={"model": "gpt", "effort": "low", "reason": "x"})
        self.assertIn("invalid model 'gpt'", msg)

    def test_build_command_refuses_unknown_effort(self):
        msg, _ = self.run_invalid(["do a thing"], decision={"model": "opus", "effort": "max", "reason": "x"})
        self.assertIn("invalid effort 'max'", msg)

    def test_all_allowed_values_pass(self):
        for model in route.MODELS:
            for effort in route.EFFORTS:
                cmd = route.build_command({"model": model, "effort": effort}, [], False, "p")
                self.assertEqual(cmd, ["claude", "--model", model, "--effort", effort, "p"])

    def test_schema_matches_allowed_values(self):
        props = route.SCHEMA["properties"]
        self.assertEqual(tuple(props["model"]["enum"]), route.MODELS)
        self.assertEqual(tuple(props["effort"]["enum"]), route.EFFORTS)


class FloorTests(unittest.TestCase):
    def run_main(self, argv, decision):
        with mock.patch.object(sys, "argv", ["route", *argv]), \
             mock.patch.object(route, "classify", return_value=dict(decision)) as classify, \
             mock.patch.object(route, "log") as log, \
             mock.patch.object(route.os, "execvp") as execvp, \
             mock.patch.object(sys, "stdin", io.StringIO()), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            route.main()
        self.stderr = err.getvalue()
        return classify, log, execvp

    def routed(self, argv, model, effort="low"):
        _, log, execvp = self.run_main(argv, {"model": model, "effort": effort, "reason": "r"})
        cmd = execvp.call_args.args[1]
        return (cmd[cmd.index("--model") + 1], cmd[cmd.index("--effort") + 1]), log.call_args.args[1]

    def test_default_floor_is_haiku_and_changes_nothing(self):
        for model in route.MODELS:
            (m, e), logged = self.routed(["do a thing"], model)
            self.assertEqual((m, e), (model, "low"))
            self.assertNotIn("floored", logged)

    def test_each_floor_raises_lower_tiers_and_leaves_higher_ones(self):
        for floor in route.MODELS:
            for model in route.MODELS:
                with self.subTest(floor=floor, model=model):
                    (m, e), logged = self.routed(["--floor", floor, "do a thing"], model, "high")
                    raised = route.MODELS.index(model) < route.MODELS.index(floor)
                    self.assertEqual(m, floor if raised else model)
                    self.assertEqual(e, "high")  # effort kept either way
                    self.assertEqual(logged.get("floored"), True if raised else None)

    def test_raised_route_keeps_every_valid_effort(self):
        for effort in route.EFFORTS:
            (m, e), _ = self.routed(["--floor", "opus", "do a thing"], "haiku", effort)
            self.assertEqual((m, e), ("opus", effort))

    def test_apply_floor_falls_back_to_medium_on_bad_effort(self):
        d = route.apply_floor({"model": "haiku", "effort": "turbo", "reason": "r"}, "sonnet")
        self.assertEqual((d["model"], d["effort"], d["floored"]), ("sonnet", "medium", True))

    def test_effort_flag_still_overrides_raised_route(self):
        (m, e), _ = self.routed(["--floor", "sonnet", "--effort", "xhigh", "do a thing"], "haiku")
        self.assertEqual((m, e), ("sonnet", "xhigh"))

    def test_dry_run_shows_raised_route(self):
        _, _, execvp = self.run_main(["--dry-run", "--floor", "sonnet", "do a thing"],
                                     {"model": "haiku", "effort": "low", "reason": "trivial"})
        execvp.assert_not_called()
        self.assertIn("[router] → sonnet / low: trivial", self.stderr)

    def test_floor_never_changes_a_failed_classification(self):
        for floor in route.MODELS:
            _, log, execvp = self.run_main(["--floor", floor, "do a thing"], route.SESSION_DEFAULT)
            self.assertEqual(execvp.call_args.args[1], ["claude", "do a thing"])
            self.assertIsNone(log.call_args.args[1]["model"])

    def test_floor_does_not_override_forced_model(self):
        classify, _, execvp = self.run_main(["--floor", "opus", "--model", "haiku", "do a thing"],
                                            {"model": "sonnet", "effort": "low", "reason": "r"})
        classify.assert_not_called()
        self.assertEqual(execvp.call_args.args[1][1:3], ["--model", "haiku"])

    def test_invalid_floor_is_rejected_before_classifying(self):
        for bad in ("gpt-4", "", "Sonnet", "max"):
            with self.subTest(bad=bad), \
                 mock.patch.object(sys, "argv", ["route", "--floor", bad, "do a thing"]), \
                 mock.patch.object(route, "classify") as classify, \
                 mock.patch.object(route.os, "execvp") as execvp, \
                 mock.patch.object(sys, "stdin", io.StringIO()), \
                 mock.patch("sys.stderr", new_callable=io.StringIO), \
                 self.assertRaises(SystemExit) as cm:
                route.main()
            self.assertIn(f"invalid floor {bad!r}", str(cm.exception.code))
            self.assertIn("haiku, sonnet, opus", str(cm.exception.code))
            classify.assert_not_called()
            execvp.assert_not_called()

    def test_help_mentions_floor(self):
        with mock.patch.object(sys, "argv", ["route", "--help"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             self.assertRaises(SystemExit):
            route.main()
        text = " ".join(out.getvalue().split())  # argparse wraps to the terminal width
        self.assertIn("--floor FLOOR never route below this model (haiku, sonnet, opus; default haiku)", text)

    def test_help_mentions_sonnet_baseline(self):
        with mock.patch.object(sys, "argv", ["route", "--help"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             self.assertRaises(SystemExit):
            route.main()
        text = " ".join(out.getvalue().split())
        self.assertIn("--model MODEL skip routing and force a model (haiku, sonnet, opus; sonnet recommended as baseline)", text)


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
