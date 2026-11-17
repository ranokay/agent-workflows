# Agent workflows

Shared GitHub Actions for optional pull request reviews using OpenCode Go's
DeepSeek V4.1 Flash. The pilot uses PR-Agent v0.50.0, pinned by Docker image
digest. The runner image's GitHub artifact attestation was verified against
`The-PR-Agent/pr-agent` before selecting it.

## Behavior

- Opened or newly ready same-repository PRs receive `/review` and `/improve`.
  Later pushes receive `/review` only. Draft PRs and bot authors/senders skip.
- `/review`, `/improve`, and `/ask QUESTION` comments require the requester's
  current repository write, maintain, or admin permission. A trusted comment can
  review a fork. Other commands and configuration flags are not accepted.
- Authorized workflow runs cancel only older runs of this workflow for the same
  PR, using monotonically increasing run numbers instead of job scheduling order.
  Actions-write permission is confined to the authorization job; the inference
  job cannot cancel workflows. Before inference the
  runner checks the current PR head, so obsolete automatic events skip.
- Code suggestions publish a summary with usage and coverage details, plus
  GitHub's committable format for suggestions passing the same score threshold.
  Applying and testing them
  remains the responsibility of a human or implementation agent. PR-Agent cannot
  push commits, approve, merge, rewrite descriptions, or resolve human threads.
- Missing model credentials skip safely. Inference errors produce a failed
  optional job instead of claiming a successful review. Do not require this job
  in branch protection; local reviews and repository tests remain the gates.

## Budgets and coverage

The job timeout is ten minutes; the runner reserves its final minute for cleanup.
Review can use up to three chunks; suggestions can use up to two chunks and each
nonempty suggestion chunk also receives self-reflection. A transient inference
failure permits one retry, without another provider or fallback model. These
limits are not a hard total model-call or dollar cap. Actual model/token/call
details and partial coverage appear in the PR comments. OpenCode Go's own
allowance and extra-usage settings still apply.

Self-reflection failures receive score zero, below the suggestion threshold of
five, so unvetted suggestions are discarded. Shared guidance includes correctness,
security, requirements, cleanup and style, distinguishing optional improvements
from defects. Dynamic context expands nearby code; it is not whole-repository
navigation or an independent proof of correctness.

## Project configuration

Store an OpenCode API key in `PR_AGENT_OPENCODE_KEY` in each consuming repository.
The key needs only **Models → Request** in the workspace subscribed to Go.
Never put a key in this repository. The caller supplies an explicit list of up
to ten project documents, read by PR-Agent from the project's default branch.
For example, use `AGENTS.md`, `CODING_STANDARDS.md`, architecture notes and a
dedicated `docs/agents/pr-review.md`. Missing documents are not fabricated.

The runner uses a stable session identifier per repository/PR and identifies
itself honestly to OpenCode. Model, retry, privilege and budget settings are
host-controlled. `.pr_agent.toml` is deliberately disabled in this pilot; project
customization belongs in the selected review documents, which cannot change
these runtime settings.

Add a caller workflow like this, replacing the pinned commit only after reviewing
a shared release. `issue_comment` events are delivered to workflows on the
default branch, so that part starts working when this caller is merged:

```yaml
name: Optional PR-Agent review
run-name: 'PR-Agent #${{ github.event.pull_request.number || github.event.issue.number }}'
on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  issue_comment:
    types: [created]
permissions:
  actions: write
  contents: read
  pull-requests: write
jobs:
  review:
    if: >-
      github.event.sender.type != 'Bot' &&
      ((github.event_name == 'pull_request' &&
        github.event.pull_request.head.repo.full_name == github.repository &&
        github.event.pull_request.user.type != 'Bot' &&
        !github.event.pull_request.draft) ||
       (github.event_name == 'issue_comment' && github.event.issue.pull_request &&
        (startsWith(github.event.comment.body, '/review') ||
         startsWith(github.event.comment.body, '/improve') ||
         startsWith(github.event.comment.body, '/ask '))))
    uses: ranokay/agent-workflows/.github/workflows/pr-agent.yml@c9cfc646c4e6fbb5efaa44642a64a23a06c8b336
    with:
      context-files: '["AGENTS.md","CODING_STANDARDS.md","docs/agents/pr-review.md"]'
    secrets:
      opencode-key: ${{ secrets.PR_AGENT_OPENCODE_KEY }}
```

The reusable workflow pins the shared implementation separately from its own
workflow commit. Consumers pin the workflow commit; neither workflow code nor
the runner is fetched from a moving branch. To release a change, commit the
runner/guidance/tests, then update the workflow's implementation pin to that
commit and publish the workflow commit. Update consumers deliberately.

## Validation

Run `mise run check` for authorization/routing tests and actionlint. Tests cover
fork approval, current permissions, bot authors, stale heads, command injection,
missing credentials, and runtime failure reporting. The Navidrome pilot also
exercises the pinned image against a real PR. `issue_comment` delivery only
starts after the caller workflow exists on a consuming repo's default branch.

Upstream behavior is version-sensitive: v0.50 keeps `/review` and `/improve`
separate and uses self-reflection. Validate new releases against their tagged
source before changing the image digest.
