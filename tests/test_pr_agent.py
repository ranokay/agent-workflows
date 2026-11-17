import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("pr_agent_runner", Path(__file__).resolve().parents[1] / "scripts/pr_agent.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

REPOSITORY = "owner/project"


def pull_request():
    return {"number": 42, "state": "open", "draft": False, "user": {"type": "User"},
            "head": {"sha": "a" * 40, "repo": {"full_name": REPOSITORY}}}


def event():
    return {"repository": {"full_name": REPOSITORY}, "sender": {"type": "User", "login": "maintainer"},
            "number": 42, "action": "opened", "pull_request": pull_request()}


def comment(body="/review"):
    result = event()
    result.update(action="created", issue={"number": 42, "pull_request": {"url": "api-url"}},
                  comment={"body": body})
    result.pop("pull_request")
    return result


class API:
    def __init__(self, pr=None, permission="write"):
        self.pr = pr or pull_request()
        self.permission = permission
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path.endswith("/permission"):
            return {"permission": self.permission}
        if path == f"repos/{REPOSITORY}/pulls/42":
            return copy.deepcopy(self.pr)
        raise AssertionError(path)


class EventAuthorizationTests(unittest.TestCase):
    def test_initial_ready_and_push_events_use_the_live_pr(self):
        for action in ("opened", "reopened", "ready_for_review", "synchronize"):
            with self.subTest(action=action):
                payload = event()
                payload.update(action=action, before="b" * 40, after="a" * 40)
                normalized, _ = runner.authorize("pull_request_target", payload, REPOSITORY, API())
                self.assertEqual(normalized["pull_request"], pull_request())

    def test_bot_author_is_rejected_even_when_a_human_reopens_it(self):
        pr = pull_request()
        pr["user"]["type"] = "Bot"
        self.assertIsNone(runner.authorize("pull_request", event(), REPOSITORY, API(pr))[0])

    def test_a_maintainer_command_also_skips_draft_prs(self):
        pr = pull_request()
        pr["draft"] = True
        self.assertIsNone(runner.authorize("issue_comment", comment(), REPOSITORY, API(pr))[0])

    def test_closed_draft_fork_and_superseded_automatic_runs_are_skipped(self):
        cases = []
        for field, value in (("state", "closed"), ("draft", True)):
            pr = pull_request()
            pr[field] = value
            cases.append(pr)
        pr = pull_request()
        pr["head"]["repo"]["full_name"] = "contributor/fork"
        cases.append(pr)
        pr = pull_request()
        pr["head"]["sha"] = "b" * 40
        cases.append(pr)
        for pr in cases:
            self.assertIsNone(runner.authorize("pull_request", event(), REPOSITORY, API(pr))[0])

    def test_bot_sender_and_ordinary_comments_spend_no_api_calls(self):
        for payload in (comment("hello"), comment("/describe")):
            api = API()
            self.assertIsNone(runner.authorize("issue_comment", payload, REPOSITORY, api)[0])
            self.assertEqual(api.calls, [])
        payload = event()
        payload["sender"]["type"] = "Bot"
        api = API()
        self.assertIsNone(runner.authorize("pull_request", payload, REPOSITORY, api)[0])
        self.assertEqual(api.calls, [])

    def test_only_current_write_permissions_can_request_fork_reviews(self):
        pr = pull_request()
        pr["head"]["repo"]["full_name"] = "contributor/fork"
        for permission in ("none", "read", "triage", "write", "maintain", "admin"):
            with self.subTest(permission=permission):
                normalized, _ = runner.authorize("issue_comment", comment(), REPOSITORY, API(pr, permission))
                self.assertEqual(normalized is not None, permission in {"write", "maintain", "admin"})

    def test_comment_commands_cannot_override_configuration_or_chain_commands(self):
        for body in ("/review --config.model=other", "/improve --extended", "/review\n/improve",
                     "/ask why --config.ai_timeout=600", "/ask ", "/ask " + "x" * 2000):
            self.assertIsNone(runner.manual_command(body), body)
        self.assertEqual(runner.manual_command(" /review\n"), "/review")
        self.assertEqual(runner.manual_command("/ask Why is this branch needed?"), "/ask Why is this branch needed?")

    def test_wrong_repository_cannot_redirect_the_token(self):
        with self.assertRaises(ValueError):
            runner.authorize("pull_request", event(), "other/repository", API())


class RuntimeTests(unittest.TestCase):
    def test_only_older_runs_of_this_workflow_and_pr_can_be_cancelled(self):
        class RunsAPI:
            def __init__(self):
                self.cancelled = []

            def get(self, path):
                if path.endswith("/runs/123"):
                    return {"workflow_id": 7, "run_number": 10}
                if "status=queued" in path:
                    return {"workflow_runs": [{"id": 6, "run_number": 6, "display_title": "PR-Agent #42"}]}
                self.assert_workflow(path)
                return {"workflow_runs": [
                    {"id": 1, "run_number": 9, "pull_requests": [{"number": 42}]},
                    {"id": 2, "run_number": 11, "pull_requests": [{"number": 42}]},
                    {"id": 3, "run_number": 8, "pull_requests": [{"number": 43}]},
                    {"id": 123, "run_number": 10, "pull_requests": [{"number": 42}]},
                ]}

            def assert_workflow(self, path):
                if "/workflows/7/runs?" not in path:
                    raise AssertionError(path)

            def cancel_run(self, repository, run_id):
                self.cancelled.append((repository, run_id))

        api = RunsAPI()
        runner.cancel_older_runs(api, REPOSITORY, 42, "123")
        self.assertEqual(api.cancelled, [(REPOSITORY, 1), (REPOSITORY, 6)])

    def test_authorization_runs_without_model_credentials_and_only_allows_valid_requests(self):
        for payload, permission, allowed in ((comment(), "write", True), (comment(), "read", False),
                                              (comment("hello"), "admin", False)):
            with self.subTest(permission=permission, command=payload["comment"]["body"]), \
                    tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                event_path = path / "event.json"
                event_path.write_text(json.dumps(payload))
                outputs = path / "outputs"
                environment = {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": "issue_comment",
                               "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_TOKEN": "test-token",
                               "GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(path / "summary"),
                               "GITHUB_RUN_ID": "123"}
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(runner, "GitHub", return_value=API(permission=permission)), \
                        patch.object(runner, "cancel_older_runs") as cancel, \
                        patch.object(runner.subprocess, "run") as run:
                    self.assertEqual(runner.main(authorize_only=True), 0)
                    run.assert_not_called()
                    self.assertEqual(cancel.called, allowed)
                lines = outputs.read_text().splitlines()
                self.assertEqual("authorized=true" in lines, allowed)

    def test_missing_model_key_skips_without_contacting_github_or_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True), \
                    patch.object(runner, "GitHub") as api, patch.object(runner.subprocess, "run") as run:
                self.assertEqual(runner.main(), 0)
                api.assert_not_called()
                run.assert_not_called()
            self.assertIn("Skipped", summary.read_text())

    def test_context_paths_cannot_escape_the_repository(self):
        for value in ('["../key"]', '["/tmp/key"]', '[5]', '{}'):
            with self.assertRaises(ValueError):
                runner.context_paths(value)
        self.assertEqual(json.loads(runner.context_paths('["AGENTS.md","docs/adr/*.md"]')),
                         ["AGENTS.md", "docs/adr/*.md"])

    def test_model_credentials_are_inherited_not_embedded_in_the_process_arguments(self):
        settings = runner.policy(REPOSITORY, 42, '["AGENTS.md"]')
        command = runner.docker_command(Path("/tmp/event.json"), "issue_comment", settings, "test-run")
        self.assertIn("GITHUB_TOKEN", command)
        self.assertIn("OPENAI__KEY", command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("/var/run/docker.sock", " ".join(command))
        self.assertEqual(settings["config.model"], settings["config.model_reasoning"])
        self.assertEqual(json.loads(settings["config.fallback_models"]), [])
        self.assertEqual(settings["config.use_repo_settings_file"], "false")
        self.assertEqual(settings["config.restricted_mode"], "true")
        self.assertLess(int(settings["pr_code_suggestions.score_on_reflection_failure"]),
                        int(settings["pr_code_suggestions.suggestions_score_threshold"]))

    def test_authorized_commands_reach_the_container_and_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            payload = path / "event.json"
            payload.write_text(json.dumps(comment("/ask Explain the change")))
            summary = path / "summary.md"
            environment = {"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_EVENT_PATH": str(payload),
                           "GITHUB_EVENT_NAME": "issue_comment", "GITHUB_REPOSITORY": REPOSITORY,
                           "GITHUB_TOKEN": "test-token", "OPENAI__KEY": "test-model-key",
                           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "REVIEW_CONTEXT_FILES": "[]"}

            def run(command, **kwargs):
                if command[:2] == ["docker", "run"]:
                    mount = command[command.index("--mount") + 1]
                    source = mount.split("source=", 1)[1].split(",target=", 1)[0]
                    normalized = json.loads(Path(source).read_text())
                    self.assertEqual(normalized["comment"]["body"], "/ask Explain the change")
                    self.assertEqual(normalized["pull_request"]["head"]["sha"], "a" * 40)
                    return subprocess_result(1)
                self.assertEqual(command[:3], ["docker", "rm", "-f"])
                return subprocess_result(0)

            with patch.dict(os.environ, environment, clear=True), patch.object(runner, "GitHub", return_value=API()), \
                    patch.object(runner.subprocess, "run", side_effect=run):
                self.assertEqual(runner.main(), 1)
            self.assertIn("Runner exit status: `1`", summary.read_text())


def subprocess_result(code):
    return runner.subprocess.CompletedProcess([], code)


if __name__ == "__main__":
    unittest.main()
