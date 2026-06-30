from pathlib import Path
from typing import Any

import yaml

from blackcell.config import ConfigError, find_repo_root
from blackcell.control_plane.models import PlanContract, contract_from_mapping

CONTRACT_FILENAME = "blackcell.plan.yaml"


class ContractError(RuntimeError):
    pass


def find_contract_path(start: Path | None = None) -> Path:
    return find_repo_root(start) / CONTRACT_FILENAME


def load_contract(start: Path | None = None, *, path: Path | None = None) -> PlanContract:
    contract_path = path or find_contract_path(start)
    if not contract_path.exists():
        raise ContractError(f"missing {CONTRACT_FILENAME}")

    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ContractError(f"could not parse {contract_path}: {error}") from error

    if not isinstance(raw, dict):
        raise ContractError(f"{contract_path} must contain a YAML mapping")

    try:
        return contract_from_mapping(raw)
    except ValueError as error:
        raise ContractError(str(error)) from error
    except ConfigError as error:
        raise ContractError(str(error)) from error


def load_contract_mapping(start: Path | None = None, *, path: Path | None = None) -> dict[str, Any]:
    contract_path = path or find_contract_path(start)
    if not contract_path.exists():
        raise ContractError(f"missing {CONTRACT_FILENAME}")

    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ContractError(f"could not parse {contract_path}: {error}") from error

    if not isinstance(raw, dict):
        raise ContractError(f"{contract_path} must contain a YAML mapping")

    return raw
