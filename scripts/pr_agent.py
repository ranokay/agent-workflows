"""Authorize GitHub events, then run the pinned upstream PR-Agent image."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


IMAGE = "pragent/pr-agent@sha256:548e894a88071180832b2f4233f734d0acd0db1a879e2fcbaa70bac8b8372ebe"
MODEL = "openai/deepseek-v4.1-flash"
ROOT = Path(__file__).resolve().parents[1]


class GitHub:
    def __init__(self, token):
        self.token = token

    def get(self, path):
        return self.request(path)

    def cancel_run(self, repository, run_id):
        self.request(f"repos/{repository}/actions/runs/{run_id}/cancel", method="POST")

    def request(self, path, method="GET"):
        request = Request(
            "https://api.github.com/" + path,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                if response.status == 202:
                    return None
                return json.load(response)
        except HTTPError as error:
            if method == "POST" and error.code == 409:
                return None  # The older run finished while cancellation was being requested.
            raise RuntimeError(f"GitHub could not verify the request (HTTP {error.code})") from None


def manual_command(body):
    text = body.strip()
    if text in {"/review", "/improve"}:
        return text
    # No command flags or configuration overrides are accepted from comments.
    if text.startswith("/ask ") and 5 < len(text) <= 2000 and "--" not in text:
        return text
    return None


def authorize(event_name, event, repository, api):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identity")
    if event.get("repository", {}).get("full_name") != repository:
        raise ValueError("Event repository does not match the workflow")
    if event.get("sender", {}).get("type") == "Bot":
        return None, "Bot event sender"
    if event_name == "issue_comment":
        issue = event.get("issue", {})
        if event.get("action") != "created" or not issue.get("pull_request"):
            return None, "Not a new PR command"
        command = manual_command(event.get("comment", {}).get("body", ""))
        if command is None:
            return None, "Unsupported command; use /review, /improve, or /ask QUESTION"
        login = event.get("sender", {}).get("login", "")
        permission = api.get(f"repos/{repository}/collaborators/{quote(login, safe='')}/permission")
        if permission.get("permission") not in {"admin", "maintain", "write"}:
            return None, "Command requester does not have repository write access"
        number = issue.get("number")
    elif event_name in {"pull_request", "pull_request_target"}:
        if event.get("action") not in {"opened", "reopened", "ready_for_review", "synchronize"}:
            return None, "Unsupported PR event"
        number = event.get("number")
        command = None
    else:
        return None, "Unsupported event"
    if type(number) is not int or number < 1:
        raise ValueError("Invalid PR number")
    pr = api.get(f"repos/{repository}/pulls/{number}")
    if pr.get("state") != "open":
        return None, "PR is closed"
    if pr.get("user", {}).get("type") == "Bot":
        return None, "Bot-authored PR"
    if pr.get("draft"):
        return None, "Draft PR"
    if event_name != "issue_comment":
        if pr.get("head", {}).get("repo", {}).get("full_name") != repository:
            return None, "Fork PR requires a maintainer command"
        if event.get("pull_request", {}).get("head", {}).get("sha") != pr["head"]["sha"]:
            return None, "A newer PR head superseded this event"
        if event.get("action") == "synchronize" and event.get("before") == event.get("after"):
            return None, "Push did not change the head"
    normalized = dict(event)
    normalized["pull_request"] = pr
    if command:
        normalized["comment"] = {**event["comment"], "body": command}
    return normalized, "Authorized"


def context_paths(value):
    paths = json.loads(value)
    if not isinstance(paths, list) or len(paths) > 10:
        raise ValueError("Context must be a JSON list of at most ten repository paths")
    for path in paths:
        if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError("Context paths must stay inside the repository")
    return json.dumps(paths)


def cancel_older_runs(api, repository, number, run_id):
    current = api.get(f"repos/{repository}/actions/runs/{run_id}")
    candidates = set()
    page = 1
    # Separate status queries can miss runs transitioning between jobs or queues.
    while True:
        runs = api.get(f"repos/{repository}/actions/workflows/{current['workflow_id']}/runs"
                       f"?per_page=100&page={page}")["workflow_runs"]
        for run in runs:
            same_pr = (any(pr.get("number") == number for pr in run.get("pull_requests", []))
                       or run.get("display_title") == f"PR-Agent #{number}")
            if same_pr and run["run_number"] < current["run_number"] and run["status"] != "completed":
                candidates.add(run["id"])
        if len(runs) < 100:
            break
        page += 1
    print(f"Cancellation scan for PR #{number}, run {run_id}: {sorted(candidates)}", flush=True)
    for candidate in sorted(candidates):
        print(f"Requesting cancellation of run {candidate}", flush=True)
        api.cancel_run(repository, candidate)
    pending = candidates.copy()
    for attempt in range(6):
        for candidate in sorted(pending):
            state = api.get(f"repos/{repository}/actions/runs/{candidate}")
            print(f"Run {candidate}: {state['status']} ({state.get('conclusion')})", flush=True)
            if state["status"] == "completed":
                pending.remove(candidate)
        if not pending:
            return
        if attempt < 5:
            time.sleep(2)
    raise RuntimeError(f"Older runs have not stopped: {sorted(pending)}; refusing overlapping inference")


def policy(repository, number, paths):
    return {
        "OPENAI__API_BASE": "https://opencode.ai/zen/go/v1",
        "LITELLM.EXTRA_HEADERS": json.dumps({
            "x-opencode-session": f"pr-agent-{repository.replace('/', '-')}-{number}",
            "user-agent": "ranokay-pr-agent/0.50.0",
        }),
        "config.model": MODEL,
        "config.model_weak": MODEL,
        "config.model_reasoning": MODEL,
        "config.fallback_models": "[]",
        "config.custom_model_max_tokens": "32000",
        "config.max_model_tokens": "32000",
        "config.num_retries": "1",
        "config.retry_same_model_on_timeout": "false",
        "config.retry_same_model_on_length": "false",
        "config.ai_timeout": "300",
        "config.use_repo_settings_file": "false",
        "config.repo_context_files": context_paths(paths),
        "config.repo_context_from_default_branch": "true",
        "config.repo_context_max_lines": "500",
        "config.system_instructions": (ROOT / "guidance/review.md").read_text(),
        "config.output_run_details": "true",
        "config.propagate_tool_errors": "true",
        "config.restricted_mode": "true",
        "config.enable_auto_approval": "false",
        "config.persistent_inline_comments": "true",
        "config.log_level": "INFO",
        "pr_reviewer.enable_large_pr_chunking": "true",
        "pr_reviewer.max_number_of_calls": "3",
        "pr_reviewer.num_max_findings": "6",
        "pr_reviewer.enable_review_labels_security": "false",
        "pr_reviewer.enable_review_labels_effort": "false",
        "pr_reviewer.enable_review_coverage_footer": "true",
        "pr_code_suggestions.committable_code_suggestions": "false",
        "pr_code_suggestions.dual_publishing_score_threshold": "5",
        "pr_code_suggestions.focus_only_on_problems": "false",
        "pr_code_suggestions.max_number_of_calls": "2",
        "pr_code_suggestions.num_code_suggestions_per_chunk": "3",
        "pr_code_suggestions.suggestions_score_threshold": "5",
        "pr_code_suggestions.score_on_reflection_failure": "0",
        "pr_code_suggestions.enable_suggestions_coverage_footer": "true",
        "pr_questions.resolve_threads": "false",
        "github_action_config.auto_review": "true",
        "github_action_config.auto_describe": "false",
        "github_action_config.auto_improve": "true",
        "github_action_config.handle_push_trigger": "true",
        "github_action_config.push_commands": '["/review"]',
        "github_action_config.fail_on_tool_errors": "true",
        "github.publish_as_check_run": "false",
    }


def docker_command(event_path, event_name, settings, name):
    command = ["docker", "run", "--rm", "--name", name,
               "--mount", f"type=bind,source={event_path},target=/event.json,readonly",
               "-e", "GITHUB_TOKEN", "-e", "OPENAI__KEY",
               "-e", "GITHUB_ACTIONS=true",
               "-e", f"GITHUB_EVENT_NAME={event_name}", "-e", "GITHUB_EVENT_PATH=/event.json"]
    for key, value in settings.items():
        command.extend(["-e", f"{key}={value}"])
    return command + [IMAGE]


def main(authorize_only=False):
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    summary.write_text("## Optional PR-Agent review\n\n")
    if authorize_only:
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("authorized=false\n")
    if not authorize_only and not os.environ.get("OPENAI__KEY"):
        summary.write_text(summary.read_text() + "Skipped: PR_AGENT_OPENCODE_KEY is not configured.\n")
        return 0
    api = GitHub(os.environ["GITHUB_TOKEN"])
    event_name = os.environ["GITHUB_EVENT_NAME"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    normalized, reason = authorize(event_name, event, repository, api)
    if normalized is None:
        print(reason)
        with summary.open("a") as output:
            output.write(f"Skipped: {reason}.\n")
        return 0
    pr = normalized["pull_request"]
    if authorize_only:
        cancel_older_runs(api, repository, pr["number"], os.environ["GITHUB_RUN_ID"])
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"authorized=true\nnumber={pr['number']}\n")
        with summary.open("a") as output:
            output.write(f"Authorized request for {repository}#{pr['number']}.\n")
        return 0
    settings = policy(repository, pr["number"], os.environ["REVIEW_CONTEXT_FILES"])
    with summary.open("a") as output:
        output.write(f"PR: {repository}#{pr['number']}\n\nHead: `{pr['head']['sha']}`\n\n")
        output.write("PR-Agent v0.50.0 · OpenCode Go / DeepSeek V4.1 Flash.\n\n")
        output.write("Limits: 3 review chunks, 2 suggestion chunks, 1 transient retry, 5-minute request timeout, 10-minute job timeout. "
                     "Self-reflection adds calls; these are not a total API-call cap. "
                     "Actual model usage and any omitted coverage appear in the PR-Agent comments.\n\n")
    with tempfile.TemporaryDirectory(prefix="pr-agent-event-") as directory:
        event_path = Path(directory) / "event.json"
        event_path.write_text(json.dumps(normalized))
        name = "pr-agent-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
        try:
            result = subprocess.run(docker_command(event_path, event_name, settings, name), timeout=540)
            status = result.returncode
        except subprocess.TimeoutExpired:
            status = 124
        finally:
            # This name belongs only to this workflow attempt; cancellation must stop its inference client.
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
    with summary.open("a") as output:
        output.write(f"Runner exit status: `{status}`. A failed or partial review is optional coverage, not a merge gate.\n")
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-only", action="store_true")
    arguments = parser.parse_args()
    try:
        sys.exit(main(arguments.authorize_only))
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"PR-Agent could not run: {error}", file=sys.stderr)
        sys.exit(1)
