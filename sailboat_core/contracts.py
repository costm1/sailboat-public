"""Small public contracts. Domain meanings and authority belong to adapters."""
from dataclasses import dataclass, field, asdict
from typing import Protocol, Any
import hashlib
import json
import math

JSON = dict[str, Any]


class SailboatError(ValueError):
    pass


class IdentityConflict(SailboatError):
    pass


class Busy(SailboatError):
    pass


def finite(value: Any) -> None:
    """Reject non-JSON data and non-finite numbers rather than coercing them."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SailboatError("non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            finite(item)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            finite(item)
        return
    raise SailboatError("JSON values and string object keys required")


def canonical(value: Any) -> str:
    # Adapted from the original controller's canonical serialization helper.
    finite(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def clone(value: Any) -> Any:
    return json.loads(canonical(value))


@dataclass(frozen=True)
class Contribution:
    source: str
    message_id: str
    text: str
    observed_at: float
    annotations: JSON = field(default_factory=dict)

    def to_dict(self) -> JSON:
        return asdict(self)


@dataclass(frozen=True)
class Evidence:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Unresolved:
    evidence: Evidence
    reason: str


@dataclass(frozen=True)
class Block:
    id: str
    type: str
    payload: JSON
    evidence: list[Evidence]
    uncertainty: str = "unspecified"
    unresolved: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Recognition:
    blocks: list[Block]
    unresolved: list[Unresolved] = field(default_factory=list)


@dataclass(frozen=True)
class Catalog:
    domain_id: str
    version: str
    recognizer_version: str
    block_schemas: dict[str, JSON]
    capabilities: dict[str, str]


class DomainPackage(Protocol):
    catalog: Catalog

    def recognize(self, contribution: Contribution) -> Recognition: ...


class Resolver(Protocol):
    resolver_id: str
    version: str
    domain_id: str

    def resolve(self, bundle: JSON, context: JSON) -> list[JSON]: ...


class ControllerPort(Protocol):
    controller_id: str
    domain_id: str

    def accept(self, bundle: JSON, delivery_id: str) -> JSON: ...

    def receipt(self, delivery_id: str) -> JSON | None: ...
