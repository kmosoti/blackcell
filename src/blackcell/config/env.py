from pathlib import Path


def read_shell_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE shell env files without executing them."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip().strip("\"'")
        if key:
            values[key] = value

    return values
