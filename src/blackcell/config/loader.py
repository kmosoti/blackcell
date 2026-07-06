from pathlib import Path


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
