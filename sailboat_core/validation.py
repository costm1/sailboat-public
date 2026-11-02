"""Structural checks, evidence coverage, and safe local-only JSON schemas."""
from dataclasses import asdict
from jsonschema import Draft202012Validator
from .contracts import Catalog, Contribution, Recognition, SailboatError, canonical, clone

CONTRIBUTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["source", "message_id", "text", "observed_at", "annotations"],
    "properties": {
        "source": {"type": "string", "minLength": 1, "maxLength": 200},
        "message_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "text": {"type": "string", "minLength": 1, "maxLength": 16000},
        "observed_at": {"type": "number", "minimum": 0},
        "annotations": {"type": "object"},
    },
}
CONTROLLER_STATUSES = {"ACCEPTED", "PENDING", "PAUSED", "COMPLETED", "REJECTED", "FAILED"}
TERMINAL = {"COMPLETED", "REJECTED", "FAILED"}


def contribution(value: Contribution) -> dict:
    data = value.to_dict()
    canonical(data)
    Draft202012Validator(CONTRIBUTION_SCHEMA).validate(data)
    if not data["text"].strip() or not data["source"].strip() or not data["message_id"].strip():
        raise SailboatError("nonblank source, message ID, and text required")
    return clone(data)


def local_schema(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef") and (not isinstance(item, str) or not item.startswith("#")):
                raise SailboatError("schema references must be local fragments; network retrieval is forbidden")
            local_schema(item)
    elif isinstance(value, list):
        for item in value:
            local_schema(item)


def catalog(value: Catalog) -> dict:
    data = asdict(value)
    canonical(data)
    if any(not isinstance(data[k], str) or not data[k].strip() for k in ("domain_id", "version", "recognizer_version")):
        raise SailboatError("catalog identity and versions required")
    if not value.block_schemas or set(value.block_schemas) != set(value.capabilities):
        raise SailboatError("each block schema must map to one named capability")
    for name, schema in value.block_schemas.items():
        if not isinstance(name, str) or not name or not isinstance(value.capabilities[name], str) or not value.capabilities[name]:
            raise SailboatError("nonblank block types and capabilities required")
        local_schema(schema)
        Draft202012Validator.check_schema(schema)
    return clone(data)


def evidence(value, text):
    if set(value) != {"start", "end", "text"}:
        raise SailboatError("evidence must contain start, end, and text")
    start, end = value["start"], value["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise SailboatError("invalid evidence offsets")
    if text[start:end] != value["text"]:
        raise SailboatError("evidence is not an exact source span")


def recognition(value: Recognition, text: str, spec: dict) -> dict:
    data = clone(asdict(value))
    ids = [b["id"] for b in data["blocks"]]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise SailboatError("unique nonblank block IDs required")
    covered = [False] * len(text)
    for b in data["blocks"]:
        if b["type"] not in spec["block_schemas"]:
            raise SailboatError("block type absent from catalog")
        Draft202012Validator(spec["block_schemas"][b["type"]]).validate(b["payload"])
        if b["uncertainty"] not in ("asserted", "tentative", "unspecified"):
            raise SailboatError("unsupported uncertainty label")
        if not b["evidence"]:
            raise SailboatError("source evidence required")
        if any(not isinstance(s, str) or not s for s in b["unresolved"]):
            raise SailboatError("unresolved fields must be nonblank descriptions")
        if any(link not in ids or link == b["id"] for link in b["links"]):
            raise SailboatError("block links must identify other blocks in the same bundle")
        b["capability"] = spec["capabilities"][b["type"]]
        for e in b["evidence"]:
            evidence(e, text)
            covered[e["start"]:e["end"]] = [True] * (e["end"] - e["start"])
    for u in data["unresolved"]:
        if not isinstance(u["reason"], str) or not u["reason"]:
            raise SailboatError("unresolved span requires a reason")
        evidence(u["evidence"], text)
        e = u["evidence"]
        covered[e["start"]:e["end"]] = [True] * (e["end"] - e["start"])
    # A recognizer cannot silently discard source material by returning an empty list.
    start = None
    for i in range(len(text) + 1):
        missing = i < len(text) and not covered[i] and not text[i].isspace()
        if missing and start is None:
            start = i
        if not missing and start is not None:
            data["unresolved"].append({"evidence": {"start": start, "end": i, "text": text[start:i]},
                                       "reason": "unrecognized source text"})
            start = None
    data["status"] = "NEEDS_CLARIFICATION" if data["unresolved"] or any(b["unresolved"] for b in data["blocks"]) else "READY"
    return data


def controller_receipt(value, delivery_id):
    canonical(value)
    if not isinstance(value, dict) or value.get("delivery_id") != delivery_id:
        raise SailboatError("controller receipt identity mismatch")
    if value.get("status") not in CONTROLLER_STATUSES or not isinstance(value.get("details"), dict):
        raise SailboatError("controller receipt requires supported status and details")
    return clone(value)
