from pathlib import Path

import pytest

from blackcell.config import (
    BlackcellConfig,
    ConfigError,
    ProjectRef,
    RepositoryRef,
    find_repo_root,
    load_config,
    write_config,
)


def test_config_round_trip(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    config = BlackcellConfig(
        repository=RepositoryRef(owner="kmosoti", name="blackcell", node_id="R_123"),
        project=ProjectRef(
            id="PVT_123",
            number=7,
            title="BlackCell",
            url="https://github.com/users/kmosoti/projects/7",
        ),
    )

    path = write_config(config, start=tmp_path)

    assert path == tmp_path / "blackcell.toml"
    assert load_config(tmp_path) == config


def test_config_write_requires_overwrite(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    config = BlackcellConfig(
        repository=RepositoryRef.parse("kmosoti/blackcell"),
        project=ProjectRef(id="PVT_123", title="BlackCell"),
    )

    write_config(config, start=tmp_path)

    with pytest.raises(ConfigError):
        write_config(config, start=tmp_path)


def test_find_repo_root_walks_up_from_nested_path(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "blackcell"
    nested.mkdir(parents=True)

    assert find_repo_root(nested) == tmp_path


def test_repository_ref_requires_owner_name() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        RepositoryRef.parse("blackcell")
