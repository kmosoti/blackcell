# BlackCell

BlackCell is a Python-first project workflow tool with a typed provider
interface around repo-local project config and GitHub Project operations.

## Bootstrap

```bash
uv sync --all-groups
uv run blackcell config show
uv run blackcell providers list
uv run blackcell control-plane validate
```

The CLI defaults to JSON for agent-readable output. Use `--jsonl` for
line-delimited records and `--rich` for human-oriented terminal rendering:

```bash
uv run blackcell config show
uv run blackcell --jsonl providers list
uv run blackcell --rich config show
uv run blackcell --rich control-plane validate
```

The current repository config lives in `blackcell.toml`. It binds this checkout
to the GitHub repository and the BlackCell project:

```toml
provider = "github"

[repository]
owner = "kmosoti"
name = "blackcell"
node_id = "R_kgDOTH7xUQ"

[project]
id = "PVT_kwHOAtZ1m84BcCSO"
title = "BlackCell"
number = 7
url = "https://github.com/users/kmosoti/projects/7"
```

To initialize another checkout:

```bash
uv run blackcell init \
  --repository kmosoti/blackcell \
  --repository-id R_kgDOTH7xUQ \
  --project-id PVT_kwHOAtZ1m84BcCSO \
  --project-number 7 \
  --project-title BlackCell \
  --project-url https://github.com/users/kmosoti/projects/7
```

Live GitHub API commands use `GITHUB_TOKEN` or `GH_TOKEN`.

```bash
uv run blackcell issue read 5
uv run blackcell project items
uv run blackcell --rich project items
```

If you use GitHub CLI in WSL, authenticate `gh` and export the token for
BlackCell from your shell startup file:

```bash
gh auth refresh -h github.com -s repo -s project -s read:org

cat >> ~/.zshrc <<'EOF'

# GitHub token for BlackCell
if command -v gh >/dev/null 2>&1; then
  export GH_TOKEN="${GH_TOKEN:-$(gh auth token --hostname github.com 2>/dev/null || true)}"
fi
EOF
```

BlackCell can also own a cached GitHub auth session so agent and non-interactive
shells do not need to inherit `GH_TOKEN`:

```bash
uv run blackcell auth login --client-id <github-oauth-client-id> --browser --qr
uv run blackcell auth status
uv run blackcell control-plane pr status --issue-key BCP-0008
```

Register the GitHub OAuth app with a public-safe homepage URL and callback URL,
then enable Device Flow. BlackCell uses the device code flow, so the callback URL
is registration metadata and no local callback server is required. The `--browser`
and `--qr` flags are prompt preferences; BlackCell still prints the device URL
and user code.

`uv run blackcell ...` assumes the current directory is the repository root. To
run BlackCell from another directory, pass the project explicitly:

```bash
uv --directory ~/src/blackcell run blackcell control-plane validate
```

## Control Plane

`blackcell.plan.yaml` is the durable, repo-authored planning contract. It is
separate from `blackcell.toml`, which only binds this checkout to a provider,
repository, and project ID.

```bash
uv run blackcell control-plane validate
uv run blackcell control-plane schema
uv run blackcell control-plane agent-context BCP-0001
uv run blackcell control-plane capabilities check
uv run blackcell control-plane sync
uv run blackcell control-plane sync --apply
uv run blackcell control-plane pr status --issue-key BCP-0001
uv run blackcell control-plane pr sync --issue-key BCP-0001
uv run blackcell control-plane pr sync --issue-key BCP-0001 --apply
uv run blackcell control-plane pr ready --issue-key BCP-0001
uv run blackcell control-plane pr ready --issue-key BCP-0001 --apply
```

The control-plane contract validates hierarchy, strict
status/type/priority/complexity enums, issue DAG dependencies, inherited
acceptance/readiness/done criteria, and cached GitHub GraphQL capabilities.
Sync is local-to-GitHub and dry-run by default; pass `--apply` to create or
update GitHub issues, attach them to the configured GitHub Project, and sync
Project fields for Status, Priority, Complexity, and Type.
The PR workflow is also dry-run by default; it guides local committed changes
through draft PR creation and marks the draft ready only after the contract
status is `Review Required` and configured checks pass.

Project and issue configuration details live in
[`docs/control-plane-configuration.md`](docs/control-plane-configuration.md).
The Vanguard spec-first QA workflow is documented in
[`docs/vanguard.md`](docs/vanguard.md).

The cached GitHub capability manifest lives under `generated/cache/` and can be
refreshed from GitHub's public GraphQL schema docs:

```bash
uv run blackcell control-plane capabilities refresh
```

## Development

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
uv run mutmut run
uv run ty check
```
