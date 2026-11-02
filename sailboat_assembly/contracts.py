"""Domain-free contracts; adapters are explicitly registered trusted Python code."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class FeedbackPolicy:
    report_already_completed: bool = False
    show_selected_pieces: bool = False


@dataclass(frozen=True)
class AssemblySpec:
    assembly_id: str
    version: str
    domain_id: str
    catalog_version: str
    recognizer_version: str
    slots: tuple[str, ...]
    matcher_id: str
    matcher_version: str
    policy_id: str
    policy_version: str
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Piece:
    slot: str
    value: object
    block_ids: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    status: str
    selected_id: str | None
    reason: str
    # Each candidate has ELIGIBLE, REJECTED, or PENDING assessment plus a reason.
    assessments: dict[str, dict]


class Matcher(Protocol):
    matcher_id: str
    version: str
    def match(self, bundle: dict) -> list[Piece]: ...


class SelectionPolicy(Protocol):
    policy_id: str
    version: str
    def decide(self, slot: str, candidates: list[dict], context: dict) -> Decision: ...
    def complete(self, selections: dict, context: dict) -> bool: ...
