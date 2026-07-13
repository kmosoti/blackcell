---
name: blackcell-publish
description: Safely deliver an intentional BlackCell change by validating scope, running focused and full gates, committing selected paths, and normally pushing agent/runtime-v1. Use only when the user explicitly asks to commit, push, or publish completed work; never use for force pushes, merges, releases, rebases, or destructive ref operations.
---

# BlackCell Publish

Publish only reviewed, intentional work from `agent/runtime-v1`.

## Establish Scope

1. Confirm the current branch is exactly `agent/runtime-v1` and its upstream is
   `origin/agent/runtime-v1`. Stop on a detached head, another branch, or an unexpected upstream.
2. Inspect `git status --porcelain=v2 --untracked-files=all`, the full diff, recent commits, and
   task acceptance evidence. Preserve unrelated tracked and untracked work.
3. Derive the exact files belonging to the requested change. Stop and ask when ownership is
   ambiguous; never stage by broad wildcard or `git add -A`.
4. Scan the selected diff for credentials, tokens, local environment data, generated caches, and
   accidental large artifacts. Exclude them and report the issue.

## Gate The Change

Run the focused checks established during implementation, then one applicable full gate from
`blackcell.plan.yaml`. Do not rerun an unchanged failure. Diagnose, alter the hypothesis or
environment, retry once when justified, then stop with a concrete blocker.

Inspect status after verification. Generated caches may remain ignored, but no unexplained tracked
delta may enter the commit.

## Commit Intentionally

1. Stage only the exact task-owned paths.
2. Inspect `git diff --cached --check`, `git diff --cached --stat`, and the complete cached diff.
3. Create one concise conventional commit whose subject describes the delivered behavior.
4. Inspect the resulting commit and status. If a hook changes files or the commit scope is wrong,
   stop; do not amend published history without explicit user direction.

## Push Normally

Fetch `origin` and confirm the local branch descends from `origin/agent/runtime-v1`. Stop on
divergence or a remote-ahead state; do not merge, rebase, reset, or rewrite history as a workaround.

Push with `git push origin agent/runtime-v1`. Never use `--force`, alternate refspecs, wrappers,
or hidden publishers. Do not merge a pull request, create a release, publish a package, delete a
ref, or change access. Report the commit SHA, pushed ref, verification summary, and final status.
