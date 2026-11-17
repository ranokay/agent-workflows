# Agent workflows

Read README.md for the review policy and release process. Keep the shared runner
limited to GitHub event authorization and launching the pinned PR-Agent image.
Do not execute content from the PR under review or add code-writing permissions.

Run `mise run check` before publishing changes. Preserve the distinction between
fixture tests, a hosted inference run, and actual comment webhook delivery.
New implementation commits require a corresponding workflow implementation-pin
update before consumers adopt them.
