from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackcell.kernel import ArtifactIntegrityError, ArtifactStore


def test_bytes_text_and_canonical_json_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    binary = store.put_bytes(b"\x00blackcell\xff")
    text = store.put_text("evidence \N{TELESCOPE}")
    structured = store.put_json({"z": [2, 1], "a": {"reliable": True}})

    assert store.get_bytes(binary) == b"\x00blackcell\xff"
    assert store.get_text(text) == "evidence \N{TELESCOPE}"
    assert store.get_json(structured) == {"a": {"reliable": True}, "z": [2, 1]}
    assert store.verify(binary)
    assert binary.digest.startswith("sha256:")
    assert store.put_json({"a": {"reliable": True}, "z": [2, 1]}) == structured


def test_content_addressing_deduplicates_and_survives_reopen(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    first_store = ArtifactStore(root)
    first = first_store.put_text("same")
    second = first_store.put_text("same")
    reopened = ArtifactStore(root)

    assert first == second
    assert reopened.stat(first.digest) == first
    assert reopened.get_text(first.digest) == "same"


def test_corrupted_blob_fails_integrity_check(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(b"original")
    path = store.path_for(reference)
    path.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError):
        store.get_bytes(reference)
    with pytest.raises(ArtifactIntegrityError):
        store.put_bytes(b"original")


@settings(max_examples=40, deadline=None)
@given(data=st.binary(max_size=4096))
def test_binary_content_address_property(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = ArtifactStore(Path(directory) / "artifacts")
        reference = store.put_bytes(data)

        assert store.get_bytes(reference.digest) == data
        assert reference.size_bytes == len(data)
        assert store.path_for(reference).is_file()
