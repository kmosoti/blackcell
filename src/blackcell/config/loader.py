import tomllib
from pathlib import Path

from blackcell.config.models import BlackcellConfig

CONFIG_FILENAME = "blackcell.toml"


class ConfigError(RuntimeError):
    pass


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate

    raise ConfigError(f"could not find a git repository from {current}")


def find_config_path(start: Path | None = None) -> Path:
    return find_repo_root(start) / CONFIG_FILENAME


def load_config(start: Path | None = None) -> BlackcellConfig:
    path = find_config_path(start)
    if not path.exists():
        raise ConfigError(f"missing {CONFIG_FILENAME}; run `blackcell init` first")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return BlackcellConfig.from_mapping(data)


def write_config(
    config: BlackcellConfig,
    *,
    start: Path | None = None,
    overwrite: bool = False,
) -> Path:
    path = find_config_path(start)
    if path.exists() and not overwrite:
        raise ConfigError(f"{path} already exists; pass --overwrite to replace it")

    path.write_text(render_config_toml(config), encoding="utf-8")
    return path


def render_config_toml(config: BlackcellConfig) -> str:
    repository_node_id = _optional_string("node_id", config.repository.node_id)
    project_number = _optional_int("number", config.project.number)
    project_url = _optional_string("url", config.project.url)

    return (
        f'provider = "{config.provider}"\n'
        "\n"
        "[repository]\n"
        f'owner = "{config.repository.owner}"\n'
        f'name = "{config.repository.name}"\n'
        f"{repository_node_id}"
        "\n"
        "[project]\n"
        f'id = "{config.project.id}"\n'
        f'title = "{config.project.title}"\n'
        f"{project_number}"
        f"{project_url}"
    )


def _optional_string(key: str, value: str | None) -> str:
    if value is None:
        return ""
    return f'{key} = "{value}"\n'


def _optional_int(key: str, value: int | None) -> str:
    if value is None:
        return ""
    return f"{key} = {value}\n"
