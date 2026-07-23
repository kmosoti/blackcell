from __future__ import annotations

import asyncio
import json
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

import msgspec
import pytest

from blackcell.adapters.tui_cursor import FileAlphaTuiCursorStore
from blackcell.interfaces.http import AlphaEventPageResponse, AlphaEventResponse
from blackcell.interfaces.tui import (
    AlphaTuiController,
    AlphaTuiCursorCheckpoint,
    AlphaTuiCursorError,
    AlphaTuiCursorFailureCode,
    AlphaTuiCursorStore,
    AlphaTuiCursorWitness,
    AlphaTuiError,
    AlphaTuiFailureCode,
    alpha_tui_endpoint_id,
)
from tests.unit.test_alpha_tui_controller import _event, _FakeAlphaTuiClient

_ENDPOINT = "http://127.0.0.1:8080"


def test_cursor_store_round_trips_canonical_owner_only_monotonic_checkpoints(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tui-state"
    store = FileAlphaTuiCursorStore.prepare(root)
    endpoint_id = alpha_tui_endpoint_id(_ENDPOINT)
    first = _checkpoint(endpoint_id, cursor=7, witness_cursor=7)

    store.save(first)

    path = root / f"{endpoint_id}.json"
    content = path.read_bytes()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert content.endswith(b"\n")
    assert b" " not in content
    assert _ENDPOINT.encode() not in content
    assert store.load(endpoint_id) == first
    assert json.loads(content)["endpoint_id"] == endpoint_id

    store.save(first)
    advanced = _checkpoint(endpoint_id, cursor=11, witness_cursor=9)
    store.save(advanced)
    assert store.load(endpoint_id) == advanced
    with pytest.raises(AlphaTuiCursorError) as captured:
        store.save(first)
    assert captured.value.code is AlphaTuiCursorFailureCode.CURSOR_REGRESSION

    other_id = alpha_tui_endpoint_id("https://runtime.example")
    assert store.load(other_id) == AlphaTuiCursorCheckpoint(
        endpoint_id=other_id,
        cursor=0,
        witness=None,
    )


def test_cursor_store_rejects_unsafe_tampered_and_mismatched_state(tmp_path: Path) -> None:
    with pytest.raises(AlphaTuiCursorError) as captured:
        FileAlphaTuiCursorStore.prepare(Path("relative"))
    assert captured.value.code is AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY

    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir(mode=0o755)
    unsafe_root.chmod(0o755)
    with pytest.raises(AlphaTuiCursorError) as captured:
        FileAlphaTuiCursorStore.prepare(unsafe_root)
    assert captured.value.code is AlphaTuiCursorFailureCode.UNSAFE_STATE_DIRECTORY

    root = tmp_path / "state"
    store = FileAlphaTuiCursorStore.prepare(root)
    endpoint_id = alpha_tui_endpoint_id(_ENDPOINT)
    checkpoint = _checkpoint(endpoint_id, cursor=7, witness_cursor=7)
    store.save(checkpoint)
    path = root / f"{endpoint_id}.json"
    path.chmod(0o644)
    with pytest.raises(AlphaTuiCursorError) as captured:
        store.load(endpoint_id)
    assert captured.value.code is AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE

    path.chmod(0o600)
    value = json.loads(path.read_bytes())
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AlphaTuiCursorError) as captured:
        store.load(endpoint_id)
    assert captured.value.code is AlphaTuiCursorFailureCode.INVALID_CHECKPOINT

    other_id = alpha_tui_endpoint_id("https://runtime.example")
    other = _checkpoint(other_id, cursor=9, witness_cursor=9)
    other_root = tmp_path / "other-state"
    other_store = FileAlphaTuiCursorStore.prepare(other_root)
    other_store.save(other)
    path.write_bytes((other_root / f"{other_id}.json").read_bytes())
    path.chmod(0o600)
    with pytest.raises(AlphaTuiCursorError) as captured:
        store.load(endpoint_id)
    assert captured.value.code is AlphaTuiCursorFailureCode.ENDPOINT_MISMATCH

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(AlphaTuiCursorError) as captured:
        store.load(endpoint_id)
    assert captured.value.code is AlphaTuiCursorFailureCode.UNSAFE_STATE_FILE


def test_controller_resumes_verified_checkpoint_and_persists_before_advancing(
    tmp_path: Path,
) -> None:
    endpoint_id = alpha_tui_endpoint_id(_ENDPOINT)
    root = tmp_path / "state"
    file_store = FileAlphaTuiCursorStore.prepare(root)
    file_store.save(_checkpoint(endpoint_id, cursor=9, witness_cursor=7))
    position_probe = _page(after=8, limit=1, scanned=1, events=(), next_cursor=9)
    witness_probe = _page(after=6, limit=1, scanned=1, events=(_event(7),), next_cursor=7)
    refresh = _page(
        after=9,
        limit=2,
        scanned=2,
        events=(_event(10), _event(11)),
        next_cursor=11,
    )
    client = _FakeAlphaTuiClient(event_pages=[position_probe, witness_probe, refresh])
    store = _RecordingCursorStore(file_store)
    controller = AlphaTuiController(client, cursor_store=store)
    store.controller = controller
    event_loop_thread = threading.get_ident()

    async def exercise() -> None:
        await controller.connect()
        assert controller.state.cursor == 9
        assert tuple(event.cursor for event in controller.state.events) == (7,)
        await controller.refresh_events(limit=2)

    asyncio.run(exercise())

    assert [request for operation, request in client.calls if operation == "events"] == [
        (8, 1),
        (6, 1),
        (9, 2),
    ]
    assert controller.state.cursor == 11
    assert tuple(event.cursor for event in controller.state.events) == (7, 10, 11)
    assert store.cursor_observed_during_save == 9
    assert store.thread_ids and all(
        thread_id != event_loop_thread for thread_id in store.thread_ids
    )
    assert file_store.load(endpoint_id) == _checkpoint(
        endpoint_id,
        cursor=11,
        witness_cursor=11,
    )


def test_controller_rejects_missing_or_mismatched_resume_evidence_without_mutation() -> None:
    endpoint_id = alpha_tui_endpoint_id(_ENDPOINT)
    checkpoint = _checkpoint(endpoint_id, cursor=7, witness_cursor=7)
    missing = _page(after=6, limit=1, scanned=0, events=(), next_cursor=6)
    mismatched = _page(
        after=6,
        limit=1,
        scanned=1,
        events=(msgspec.structs.replace(_event(7), payload_digest="f" * 64),),
        next_cursor=7,
    )

    for page in (missing, mismatched):
        client = _FakeAlphaTuiClient(event_pages=[page])
        controller = AlphaTuiController(client, cursor_store=_MemoryCursorStore(checkpoint))
        with pytest.raises(AlphaTuiError) as captured:
            asyncio.run(controller.connect())
        assert captured.value.code is AlphaTuiFailureCode.INVALID_CURSOR_CHECKPOINT
        assert controller.state.cursor == 0
        assert controller.state.endpoint is None
        assert controller.state.connected is False
        assert controller.state.revision == 0

    empty = AlphaTuiCursorCheckpoint(endpoint_id=endpoint_id, cursor=0, witness=None)
    disconnected_client = _FakeAlphaTuiClient(event_pages=[missing])
    disconnected = AlphaTuiController(
        disconnected_client,
        cursor_store=_MemoryCursorStore(empty),
    )
    with pytest.raises(AlphaTuiError) as captured:
        asyncio.run(disconnected.refresh_events(limit=1))
    assert captured.value.code is AlphaTuiFailureCode.CURSOR_STORE_NOT_CONNECTED
    assert disconnected_client.calls == []


@dataclass(slots=True)
class _MemoryCursorStore:
    checkpoint: AlphaTuiCursorCheckpoint

    def load(self, endpoint_id: str) -> AlphaTuiCursorCheckpoint:
        assert endpoint_id == self.checkpoint.endpoint_id
        return self.checkpoint

    def save(self, checkpoint: AlphaTuiCursorCheckpoint) -> None:
        self.checkpoint = checkpoint


class _RecordingCursorStore:
    def __init__(self, delegate: AlphaTuiCursorStore) -> None:
        self.delegate = delegate
        self.controller: AlphaTuiController | None = None
        self.cursor_observed_during_save: int | None = None
        self.thread_ids: list[int] = []

    def load(self, endpoint_id: str) -> AlphaTuiCursorCheckpoint:
        self.thread_ids.append(threading.get_ident())
        return self.delegate.load(endpoint_id)

    def save(self, checkpoint: AlphaTuiCursorCheckpoint) -> None:
        self.thread_ids.append(threading.get_ident())
        assert self.controller is not None
        self.cursor_observed_during_save = self.controller.state.cursor
        self.delegate.save(checkpoint)


def _checkpoint(
    endpoint_id: str,
    *,
    cursor: int,
    witness_cursor: int,
) -> AlphaTuiCursorCheckpoint:
    return AlphaTuiCursorCheckpoint(
        endpoint_id=endpoint_id,
        cursor=cursor,
        witness=AlphaTuiCursorWitness(
            cursor=witness_cursor,
            event_id=f"event-{witness_cursor}",
            payload_digest="a" * 64,
        ),
    )


def _page(
    *,
    after: int,
    limit: int,
    scanned: int,
    events: tuple[AlphaEventResponse, ...],
    next_cursor: int,
) -> AlphaEventPageResponse:
    return AlphaEventPageResponse(
        after_cursor=after,
        limit=limit,
        scanned_events=scanned,
        events=events,
        next_cursor=next_cursor,
        has_more=scanned == limit and next_cursor < 11,
    )
