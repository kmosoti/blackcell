from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuleAtom:
    subject: str
    predicate: str
    object: str


@dataclass(frozen=True, slots=True)
class Rule:
    key: str
    head: RuleAtom
    body: tuple[RuleAtom, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class RuleSet:
    rules: tuple[Rule, ...]


@dataclass(frozen=True, slots=True)
class ValidationMessage:
    level: str
    code: str
    message: str
    path: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[ValidationMessage, ...] = ()
    warnings: tuple[ValidationMessage, ...] = ()
