#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_root="$(mktemp -d "${TMPDIR:-/tmp}/blackcell-runtime-v1-example.XXXXXX")"
repository="$work_root/repository"

cleanup() {
  chmod -R u+w "$work_root" 2>/dev/null || true
  find "$work_root" -type f -delete 2>/dev/null || true
  find "$work_root" -depth -type d -empty -delete 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$repository"
git -C "$repository" init --quiet
git -C "$repository" config user.name "BlackCell recorded example"
git -C "$repository" config user.email "recorded@example.invalid"
printf '%s\n' '# Recorded runtime-v1 example' > "$repository/README.md"
git -C "$repository" add README.md
git -C "$repository" commit --quiet -m "Initialize recorded example"

cd "$project_root"
uv run blackcell operator run --model recorded --repo "$repository" > "$work_root/run.json"
uv run blackcell operator state --repo "$repository" > "$work_root/state.json"
uv run blackcell operator replay --repo "$repository" > "$work_root/replay.json"

uv run python - "$work_root/run.json" "$work_root/state.json" "$work_root/replay.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
state = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
replay = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))

assert run["workflow_version"] == "daily-operator/v2"
assert run["status"] == "completed"
assert replay["protocol_version"] == "daily-operator/v2"
assert replay["classification"] == "completed"
assert replay["finding"] is None
assert replay["run_id"] == run["run_id"]
assert state["cutoff_global_position"] > 0

print(
    json.dumps(
        {
            "replay": replay["classification"],
            "run": run["status"],
            "schema_version": "runtime-v1-recorded-example/v1",
            "state_projected": True,
            "workflow_version": run["workflow_version"],
        },
        sort_keys=True,
    )
)
PY
