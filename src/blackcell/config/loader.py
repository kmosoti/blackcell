"""TOML configuration discovery and validation."""

import os
import tomllib
from pathlib import Path

from pydantic import ValidationError

from blackcell.config.model import BlackcellConfig
from blackcell.contracts.errors import ValidationFailure


def find_config(explicit: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if environment_path := os.environ.get("BLACKCELL_CONFIG"):
        candidates.append(Path(environment_path))
    candidates.extend(
        [Path.cwd() / "blackcell.toml", Path.home() / ".config/blackcell/config.toml"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValidationFailure(
        "blackcell.toml was not found.",
        recovery="Run from the repository root or set BLACKCELL_CONFIG.",
    )


def load_config(path: str | Path | None = None) -> BlackcellConfig:
    config_path = find_config(path)
    try:
        with config_path.open("rb") as handle:
            return BlackcellConfig.model_validate(tomllib.load(handle))
    except ValidationError as error:
        failures = [
            {
                "location": ".".join(str(component) for component in item["loc"]),
                "type": item["type"],
                "message": item["msg"],
            }
            for item in error.errors(include_input=False, include_url=False)
        ]
        raise ValidationFailure(
            "Invalid Blackcell configuration.",
            details={"failures": failures},
        ) from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValidationFailure(f"Invalid Blackcell configuration: {error}") from error
