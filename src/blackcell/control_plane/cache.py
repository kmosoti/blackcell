import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from blackcell.config import find_repo_root

CONTROL_PLANE_CACHE_PATH = Path("generated/cache/control_plane.sqlite3")


@dataclass(frozen=True, slots=True)
class SyncCacheEntry:
    issue_key: str
    repository_id: str
    project_id: str
    issue_id: str
    issue_number: int
    issue_url: str
    project_item_id: str | None
    contract_digest: str
    body_digest: str
    synced_at: str
    adopted_at: str | None = None
    adoption_source: str | None = None
    prior_remote_digest: str | None = None


@dataclass(frozen=True, slots=True)
class PullRequestCacheEntry:
    issue_key: str
    repository_id: str
    project_id: str
    pull_request_id: str
    pull_request_number: int
    pull_request_url: str
    issue_id: str | None
    project_item_id: str | None
    base_ref_name: str
    head_ref_name: str
    head_ref_oid: str
    body_digest: str
    is_draft: bool
    synced_at: str
    ready_at: str | None = None


class ControlPlaneSyncCache:
    def __init__(self, path: Path, *, create: bool = True) -> None:
        self.path = path
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path)
        else:
            self._connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row
        if create:
            self._migrate()

    @classmethod
    def open(
        cls,
        *,
        start: Path | None = None,
        path: Path | None = None,
        create: bool = True,
    ) -> ControlPlaneSyncCache | None:
        cache_path = path or find_repo_root(start) / CONTROL_PLANE_CACHE_PATH
        if not create and not cache_path.exists():
            return None
        return cls(cache_path, create=create)

    def close(self) -> None:
        self._connection.close()

    def get(
        self,
        issue_key: str,
        *,
        repository_id: str,
        project_id: str,
    ) -> SyncCacheEntry | None:
        row = self._connection.execute(
            """
            select *
            from issue_sync
            where issue_key = ?
              and repository_id = ?
              and project_id = ?
            """,
            (issue_key, repository_id, project_id),
        ).fetchone()
        if row is None:
            return None
        return _entry_from_row(row)

    def upsert(self, entry: SyncCacheEntry) -> None:
        self._connection.execute(
            """
            insert into issue_sync (
              issue_key,
              repository_id,
              project_id,
              issue_id,
              issue_number,
              issue_url,
              project_item_id,
              contract_digest,
              body_digest,
              synced_at,
              adopted_at,
              adoption_source,
              prior_remote_digest
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue_key) do update set
              repository_id = excluded.repository_id,
              project_id = excluded.project_id,
              issue_id = excluded.issue_id,
              issue_number = excluded.issue_number,
              issue_url = excluded.issue_url,
              project_item_id = excluded.project_item_id,
              contract_digest = excluded.contract_digest,
              body_digest = excluded.body_digest,
              synced_at = excluded.synced_at,
              adopted_at = coalesce(issue_sync.adopted_at, excluded.adopted_at),
              adoption_source = coalesce(issue_sync.adoption_source, excluded.adoption_source),
              prior_remote_digest = coalesce(
                issue_sync.prior_remote_digest,
                excluded.prior_remote_digest
              )
            """,
            (
                entry.issue_key,
                entry.repository_id,
                entry.project_id,
                entry.issue_id,
                entry.issue_number,
                entry.issue_url,
                entry.project_item_id,
                entry.contract_digest,
                entry.body_digest,
                entry.synced_at,
                entry.adopted_at,
                entry.adoption_source,
                entry.prior_remote_digest,
            ),
        )
        self._connection.commit()

    def get_pull_request(
        self,
        issue_key: str,
        *,
        repository_id: str,
        project_id: str,
    ) -> PullRequestCacheEntry | None:
        row = self._connection.execute(
            """
            select *
            from pull_request_sync
            where issue_key = ?
              and repository_id = ?
              and project_id = ?
            """,
            (issue_key, repository_id, project_id),
        ).fetchone()
        if row is None:
            return None
        return _pull_request_entry_from_row(row)

    def upsert_pull_request(self, entry: PullRequestCacheEntry) -> None:
        self._connection.execute(
            """
            insert into pull_request_sync (
              issue_key,
              repository_id,
              project_id,
              pull_request_id,
              pull_request_number,
              pull_request_url,
              issue_id,
              project_item_id,
              base_ref_name,
              head_ref_name,
              head_ref_oid,
              body_digest,
              is_draft,
              synced_at,
              ready_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue_key) do update set
              repository_id = excluded.repository_id,
              project_id = excluded.project_id,
              pull_request_id = excluded.pull_request_id,
              pull_request_number = excluded.pull_request_number,
              pull_request_url = excluded.pull_request_url,
              issue_id = excluded.issue_id,
              project_item_id = excluded.project_item_id,
              base_ref_name = excluded.base_ref_name,
              head_ref_name = excluded.head_ref_name,
              head_ref_oid = excluded.head_ref_oid,
              body_digest = excluded.body_digest,
              is_draft = excluded.is_draft,
              synced_at = excluded.synced_at,
              ready_at = coalesce(pull_request_sync.ready_at, excluded.ready_at)
            """,
            (
                entry.issue_key,
                entry.repository_id,
                entry.project_id,
                entry.pull_request_id,
                entry.pull_request_number,
                entry.pull_request_url,
                entry.issue_id,
                entry.project_item_id,
                entry.base_ref_name,
                entry.head_ref_name,
                entry.head_ref_oid,
                entry.body_digest,
                int(entry.is_draft),
                entry.synced_at,
                entry.ready_at,
            ),
        )
        self._connection.commit()

    def _migrate(self) -> None:
        self._connection.execute(
            """
            create table if not exists issue_sync (
              issue_key text primary key,
              repository_id text not null,
              project_id text not null,
              issue_id text not null,
              issue_number integer not null,
              issue_url text not null,
              project_item_id text,
              contract_digest text not null,
              body_digest text not null,
              synced_at text not null,
              adopted_at text,
              adoption_source text,
              prior_remote_digest text
            )
            """
        )
        self._connection.execute(
            """
            create table if not exists pull_request_sync (
              issue_key text primary key,
              repository_id text not null,
              project_id text not null,
              pull_request_id text not null,
              pull_request_number integer not null,
              pull_request_url text not null,
              issue_id text,
              project_item_id text,
              base_ref_name text not null,
              head_ref_name text not null,
              head_ref_oid text not null,
              body_digest text not null,
              is_draft integer not null,
              synced_at text not null,
              ready_at text
            )
            """
        )
        self._connection.commit()


def now_timestamp() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _entry_from_row(row: sqlite3.Row) -> SyncCacheEntry:
    return SyncCacheEntry(
        issue_key=row["issue_key"],
        repository_id=row["repository_id"],
        project_id=row["project_id"],
        issue_id=row["issue_id"],
        issue_number=row["issue_number"],
        issue_url=row["issue_url"],
        project_item_id=row["project_item_id"],
        contract_digest=row["contract_digest"],
        body_digest=row["body_digest"],
        synced_at=row["synced_at"],
        adopted_at=row["adopted_at"],
        adoption_source=row["adoption_source"],
        prior_remote_digest=row["prior_remote_digest"],
    )


def _pull_request_entry_from_row(row: sqlite3.Row) -> PullRequestCacheEntry:
    return PullRequestCacheEntry(
        issue_key=row["issue_key"],
        repository_id=row["repository_id"],
        project_id=row["project_id"],
        pull_request_id=row["pull_request_id"],
        pull_request_number=row["pull_request_number"],
        pull_request_url=row["pull_request_url"],
        issue_id=row["issue_id"],
        project_item_id=row["project_item_id"],
        base_ref_name=row["base_ref_name"],
        head_ref_name=row["head_ref_name"],
        head_ref_oid=row["head_ref_oid"],
        body_digest=row["body_digest"],
        is_draft=bool(row["is_draft"]),
        synced_at=row["synced_at"],
        ready_at=row["ready_at"],
    )
