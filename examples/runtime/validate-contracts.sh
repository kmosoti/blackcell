#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
request_root="$project_root/examples/runtime/requests"

cd "$project_root"
uv run python - "$request_root" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from blackcell.interfaces.http import (
    CancelRunRequest,
    IntentRequest,
    PlanRequest,
    ProjectRequest,
    RunQueryRequest,
    RunRequest,
    decode_contract,
)

root = Path(sys.argv[1])
project = decode_contract((root / "project.template.json").read_bytes(), ProjectRequest)
intent = decode_contract((root / "intent.template.json").read_bytes(), IntentRequest)
plan = decode_contract((root / "plan.template.json").read_bytes(), PlanRequest)
run = decode_contract((root / "run.template.json").read_bytes(), RunRequest)
query = decode_contract((root / "query.template.json").read_bytes(), RunQueryRequest)
cancel = decode_contract((root / "cancel.template.json").read_bytes(), CancelRunRequest)

assert intent.project_id == plan.project_id == run.project_id == project.project_id
assert plan.intent_id == run.intent_id == intent.intent_id
assert run.plan_id == plan.plan_id
assert query.project_ids == (project.project_id,)
assert query.intent_ids == (intent.intent_id,)
assert query.plan_ids == (plan.plan_id,)
assert query.run_ids == (run.run_id,)
assert len({
    project.idempotency_key,
    intent.idempotency_key,
    plan.idempotency_key,
    run.idempotency_key,
    cancel.idempotency_key,
}) == 5

print(json.dumps({
    "contracts": ["project", "intent", "plan", "run", "query", "cancel"],
    "schema_version": "runtime-contract-example",
    "status": "valid",
}, sort_keys=True))
PY
