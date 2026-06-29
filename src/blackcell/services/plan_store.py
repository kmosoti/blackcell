"""Local canonical PlanSpec storage, separate from the append-only chronicle."""

from pathlib import Path

from platformdirs import user_data_path

from blackcell.contracts.errors import NotFoundFailure
from blackcell.contracts.plan import PlanSpec, validate_plan_id


class PlanStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_data_path("blackcell") / "directives"

    def save(self, plan: PlanSpec) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{plan.plan_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(plan.canonical_bytes() + b"\n")
        temporary.replace(path)
        return path

    def load(self, plan_id: str) -> PlanSpec:
        validate_plan_id(plan_id)
        path = self.root / f"{plan_id}.json"
        if not path.is_file():
            raise NotFoundFailure(
                f"Directive {plan_id} is not in the local directive store.",
                recovery="Run blackcell directive validate or propose with the PlanSpec path.",
            )
        return PlanSpec.from_file(path)
