# Control Plane Configuration

BlackCell keeps provider binding and authored planning state separate.
`blackcell.toml` identifies the remote repository and GitHub Project. The
repo-authored planning API lives in `blackcell.plan.yaml` and is parsed into
frozen dataclasses under `blackcell.control_plane.models`.

## Configuration Sources

```mermaid
flowchart LR
    toml["blackcell.toml<br/>provider binding"] --> config["BlackcellConfig"]
    plan["blackcell.plan.yaml<br/>planning contract"] --> loader["contract loader"]
    loader --> validation["strict validation"]
    validation --> models["frozen dataclass API"]
    models --> render["deterministic issue rendering"]
    render --> provider["GitHub provider"]
    provider --> github["GitHub issues<br/>GitHub Project"]
    provider --> cache["generated/cache/control_plane.sqlite3"]
    cache --> provider

    classDef source fill:#eef6ff,stroke:#2563eb,color:#172554
    classDef api fill:#f8fafc,stroke:#475569,color:#0f172a
    classDef remote fill:#ecfdf5,stroke:#059669,color:#064e3b
    classDef cache fill:#fff7ed,stroke:#ea580c,color:#7c2d12

    class toml,plan source
    class config,loader,validation,models,render,provider api
    class github remote
    class cache cache
```

`blackcell.toml` should contain only provider state:

```toml
provider = "github"

[repository]
owner = "kmosoti"
name = "blackcell"
node_id = "R_123"

[project]
id = "PVT_123"
title = "BlackCell"
number = 7
url = "https://github.com/users/kmosoti/projects/7"
```

Remote issue IDs, issue numbers, Project item IDs, and sync digests are
generated operational state. They belong in
`generated/cache/control_plane.sqlite3`, not in `blackcell.plan.yaml`.

## Project Kinds

`blackcell.plan.yaml` models project structure with these planning node kinds:

| Kind | YAML key | Purpose |
| --- | --- | --- |
| Project | `project` | Top-level planning namespace and display name. |
| Roadmap | `roadmaps` | Long-running initiative that groups epics. |
| Epic | `epics` | Delivery area inside a roadmap. |
| Milestone | `milestones` | Target slice inside an epic. |
| Issue | `issues` | Work contract rendered to a GitHub issue. |
| Native automation | `native_automation` | Repo-local command hooks such as validation before sync. |
| Agent workflow | `agent_workflow` | Agent ownership and model routing metadata. |

```mermaid
classDiagram
    class ProjectPlan {
      +str key
      +str name
      +str? description
    }
    class Roadmap {
      +str key
      +str title
      +tuple~str~ epics
    }
    class Epic {
      +str key
      +str title
      +str roadmap
      +tuple~str~ milestones
    }
    class Milestone {
      +str key
      +str title
      +str epic
      +str? target
    }
    class IssuePlan {
      +str key
      +str title
      +IssueType kind
      +IssueStatus status
      +Priority priority
      +Complexity complexity
      +str github_title
      +bool is_done
      +bool is_active
      +bool has_dependencies
    }
    class NativeAutomation {
      +str key
      +str trigger
      +str action
      +bool enabled
    }
    class AgentWorkflow {
      +str model
      +tuple~AgentWorker~ workers
    }

    ProjectPlan "1" --> "*" Roadmap
    Roadmap "1" --> "*" Epic
    Epic "1" --> "*" Milestone
    Epic "1" --> "*" IssuePlan
    Milestone "1" --> "*" IssuePlan
    ProjectPlan "1" --> "*" NativeAutomation
    ProjectPlan "1" --> "0..1" AgentWorkflow
```

## Issue Kinds

Issue kinds are represented by the `IssueType` enum and configured with the
`type` field:

| Kind | YAML value | Intended use |
| --- | --- | --- |
| Feature | `feature` | New user-visible or platform capability. |
| Bug | `bug` | Defect fix or regression repair. |
| Refactor | `refactor` | Behavior-preserving structural change. |
| Chore | `chore` | Maintenance, dependency, tooling, or housekeeping work. |

The `IssuePlan` dataclass is frozen and slot-backed. Its constructor fields are
the YAML contract API; parser helpers such as `_issue`, `_enum`, and
`_reject_unknown` are private module internals. Public computed properties are
kept small and stable:

| Property | Meaning |
| --- | --- |
| `kind` | Alias for `type`, used by callers that describe issue categories as kinds. |
| `github_title` | Remote GitHub issue title. This is intentionally the contract title without a key prefix. |
| `is_done` | True when status is `Done`. |
| `is_active` | True for `In Progress` or `Review Required`. |
| `is_backlog` | True when status is `Backlog`. |
| `has_dependencies` | True when `depends_on` contains at least one issue key. |
| `has_scope` | True when local scope entries are configured. |
| `has_delivery_contract` | True when change spec or local ready/done/acceptance criteria exist. |
| `hierarchy_keys` | Ordered non-empty `epic` and `milestone` references. |

## Issue Configuration

```yaml
issues:
  - key: BCP-0001
    title: Define the durable planning contract
    type: feature
    status: Todo
    priority: P0
    complexity: 5
    epic: EPIC-CONTROL-PLANE
    milestone: MS-CP-1
    depends_on:
      - BCP-0000
    areas_of_responsibility:
      - contract/schema
    scope:
      - Add typed dataclasses and strict enum parsing.
    context:
      - blackcell.plan.yaml is repo-authored planning state.
    change_spec:
      - Add contract models and validators.
    acceptance_criteria:
      - Invalid enum values fail during contract load.
    definition_of_ready:
      - Scope and acceptance criteria are present.
    definition_of_done:
      - Unit tests cover success and failure paths.
```

Required fields are `key`, `title`, `type`, `status`, `priority`, and
`complexity`. Optional sequence fields default to empty immutable tuples.
Unknown fields are rejected during load.

```mermaid
flowchart LR
    start([Start]) --> backlog[Backlog]
    backlog --> todo[Todo]
    todo --> progress[In Progress]
    progress --> review[Review Required]
    review --> progress
    review --> done[Done]
    progress --> done
    done --> finish([Finish])

    note["GitHub issue open/closed state is not managed by the first sync slice."]
    done -.-> note

    classDef state fill:#f8fafc,stroke:#475569,color:#0f172a
    classDef boundary fill:#ecfdf5,stroke:#059669,color:#064e3b
    classDef note fill:#fff7ed,stroke:#ea580c,color:#7c2d12

    class backlog,todo,progress,review,done state
    class start,finish boundary
    class note note
```

## Sync Materialization

`blackcell control-plane sync` is local-to-GitHub and dry-run by default.
`--apply` creates or updates GitHub issues and ensures each issue is attached to
the configured GitHub Project. It also makes the Project representative of the
planning contract by ensuring these fields and item values:

| Field | Type | Source |
| --- | --- | --- |
| `Status` | Single select | `issues[].status` |
| `Priority` | Single select | `issues[].priority` |
| `Complexity` | Number | `issues[].complexity` |
| `Type` | Single select | `issues[].type` |

When the whole contract is synced, unmanaged GitHub issue items in the Project
are archived so the Project rows match the local contract. Targeted
`--issue-key` sync does not archive unrelated Project items.

```mermaid
sequenceDiagram
    participant CLI as blackcell control-plane sync
    participant Plan as blackcell.plan.yaml
    participant Cache as SQLite cache
    participant GH as GitHub GraphQL

    CLI->>Plan: load and validate contract
    CLI->>Cache: read issue key binding
    alt cache issue exists remotely
        CLI->>GH: read issue by node ID
    else missing or refresh requested
        CLI->>GH: discover by BlackCell marker
        CLI->>GH: discover by exact title
    end
    alt no remote issue
        CLI->>GH: create GitHub issue attached to project
    else body or title differs
        CLI->>GH: updateIssue
    end
    CLI->>GH: attach issue to GitHub Project when absent
    CLI->>GH: ensure Project fields and options
    CLI->>GH: update item Status/Priority/Complexity/Type
    opt whole-contract sync
        CLI->>GH: archive unmanaged issue items
    end
    CLI->>Cache: store node IDs and digests on apply
```

## Pull Request Workflow

`blackcell control-plane pr` is a guarded, idempotent workflow for moving one
issue from committed local changes to a reviewable pull request. It guides local
git state but does not commit, push, or merge. Mutating remote operations remain
dry-run by default unless `--apply` is passed.

| Command | Purpose |
| --- | --- |
| `blackcell control-plane pr status --issue-key BCP-0001` | Report the current workflow state and next commands. |
| `blackcell control-plane pr sync --issue-key BCP-0001` | Plan creation or update of the managed draft PR. |
| `blackcell control-plane pr sync --issue-key BCP-0001 --apply` | Create/update the managed draft PR and attach it to the GitHub Project. |
| `blackcell control-plane pr ready --issue-key BCP-0001` | Check whether the draft PR can move to review. |
| `blackcell control-plane pr ready --issue-key BCP-0001 --apply` | Mark the draft PR ready when all gates pass. |

The workflow is intentionally one issue to one pull request. The PR title is the
contract issue title. The body is rendered from the local contract and includes
hidden BlackCell markers for rediscovery. Managed PR Project items mirror the
same `Status`, `Priority`, `Complexity`, and `Type` values as their issue
contract so the Project remains scannable from either row.

```mermaid
stateDiagram
    [*] --> NeedsChanges: dirty worktree or detached HEAD
    [*] --> NeedsPush: committed branch is not pushed
    [*] --> NeedsDraftPR: issue synced and branch pushed
    NeedsChanges --> NeedsPush: user commits changes
    NeedsPush --> NeedsDraftPR: user pushes branch
    NeedsDraftPR --> DraftOpen: pr sync --apply
    DraftOpen --> ReadyBlocked: status/check gate fails
    ReadyBlocked --> DraftOpen: contract or checks fixed
    DraftOpen --> ReviewReady: pr ready --apply
    ReviewReady --> [*]

    note right of NeedsChanges
      BlackCell reports git commands only.
      It does not commit or push.
    end note

    note right of ReviewReady
      Merge policy is deferred.
      The command only moves draft to review.
    end note
```

Ready-to-review requires all of the following:

| Gate | Source |
| --- | --- |
| Clean attached branch | Local git inspection. |
| Branch pushed to its upstream | Local git inspection. |
| Managed draft PR exists | GitHub GraphQL and SQLite cache. |
| Issue status is `Review Required` | `blackcell.plan.yaml`. |
| Required checks pass | `pr_policy.required_checks`. |
| PR attached to the GitHub Project | GitHub GraphQL and SQLite cache. |

```mermaid
sequenceDiagram
    participant User as Agent or maintainer
    participant CLI as blackcell control-plane pr
    participant Git as local git
    participant Plan as blackcell.plan.yaml
    participant Cache as SQLite cache
    participant GH as GitHub GraphQL

    User->>CLI: pr sync/status/ready --issue-key
    CLI->>Git: inspect branch, HEAD, upstream, dirty state
    alt local work not committed or pushed
        CLI-->>User: needs_changes or needs_push with commands
    else branch is clean and pushed
        CLI->>Plan: load issue contract and PR policy
        CLI->>Cache: read issue and PR bindings
        CLI->>GH: rediscover issue or PR when cache is missing/stale
        alt no PR and sync --apply
            CLI->>GH: createPullRequest(draft: true)
            CLI->>GH: attach PR to GitHub Project
            CLI->>GH: sync PR item Status/Priority/Complexity/Type
            CLI->>Cache: store PR node ID and digest
        else PR differs from contract and --apply
            CLI->>GH: updatePullRequest
            CLI->>GH: sync PR item Status/Priority/Complexity/Type
            CLI->>Cache: update rendered digest
        end
        opt ready command
            CLI->>CLI: run required checks
            CLI->>Plan: require issue status Review Required
            CLI->>GH: markPullRequestReadyForReview when gates pass
        end
        CLI-->>User: JSON workflow result
    end
```

`pr_policy` configures the local readiness gate:

```yaml
pr_policy:
  require_issue_link: true
  required_checks:
    - ruff
    - ty
    - pytest
  merge_strategy: squash
```

Supported check names are `ruff`, `ruff-format`, `ty`, and `pytest`. Merge
strategy is contract metadata in this slice; merge execution is not managed.
