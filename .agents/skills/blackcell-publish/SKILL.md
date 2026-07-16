---
name: blackcell-publish
description: Safely deliver intentional BlackCell changes by validating scope, running focused and full gates, committing selected paths, and normally pushing the explicitly authorized checked-out branch to its matching origin upstream. Use only when the user explicitly asks to commit, push, or publish completed work; never use for force pushes, merges, releases, rebases, or destructive ref operations.
---

# BlackCell Publish

Publish only reviewed, intentional work from the checked-out branch explicitly authorized by the
user.

## Establish Target And Scope

1. Read the branch with `git branch --show-current`. Stop on an empty result or detached head.
2. Resolve the authorized target from the user's request and, when applicable, the active program
   branch declared in `blackcell.plan.yaml`. Stop if any declared target differs from the checked-out
   branch; never infer a different branch from old workflow text.
3. Read the upstream with `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}`. An existing
   upstream must be exactly `origin/<current-branch>`. If no upstream exists, do not choose one
   silently: creating or attaching `origin/<current-branch>` requires explicit authorization to
   publish that exact branch and a remote-head check first.
4. Inspect `git status --porcelain=v2 --untracked-files=all`, the full diff, recent commits, and
   task acceptance evidence. Preserve unrelated tracked and untracked work.
5. Derive the exact files belonging to the requested change. Stop and ask when ownership is
   ambiguous; never stage by broad wildcard or `git add -A`.
6. Scan the selected diff for credentials, tokens, local environment data, generated caches, and
   accidental large artifacts. Exclude them and report the issue.

Fetch `origin` before committing. For an existing remote branch, require
`git merge-base --is-ancestor origin/<current-branch> HEAD` to succeed. Stop on remote-ahead or
divergent history; do not merge, rebase, reset, or rewrite history as a workaround.

## Gate The Change

Run the focused checks established during implementation, then one applicable full gate from
`blackcell.plan.yaml`. Do not rerun an unchanged failure. Diagnose, alter the hypothesis or
environment, retry once when justified, then stop with a concrete blocker.

Inspect status after verification. Generated caches may remain ignored, but no unexplained tracked
delta may enter the commit.

## Commit Intentionally

1. Stage only the exact task-owned paths.
2. Inspect `git diff --cached --check`, `git diff --cached --stat`, and the complete cached diff.
3. Create one concise conventional commit per independently reviewable change. When the task
   contains separate accepted scopes, stage and commit them separately.
4. Inspect every resulting commit and status. If a hook changes files or a commit scope is wrong,
   stop; do not amend published history without explicit user direction.

## Push Normally

Fetch `origin` again and repeat the exact upstream and ancestry checks immediately before pushing.
For a new branch, require that `refs/heads/<current-branch>` is absent remotely before the explicitly
authorized first push.

Push with `git push origin <current-branch>`; replace the placeholder with the exact checked-out
branch name. Use `git push --set-upstream origin <current-branch>` only for an explicitly authorized
first publication. Never use `--force`, alternate refspecs, wrappers, or hidden publishers. Do not
merge a pull request, create a release, publish a package, delete a ref, or change access.

Fetch the exact branch after pushing and require `git rev-parse HEAD` to equal
`git rev-parse origin/<current-branch>`. Report the commit SHA, pushed ref, verification summary,
and final status.
