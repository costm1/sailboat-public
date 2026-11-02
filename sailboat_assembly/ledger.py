"""A local assembly controller, with immutable input receipts and filtered feedback."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import threading
import time

from sailboat_core.contracts import canonical, clone, digest, IdentityConflict, SailboatError
from .contracts import AssemblySpec, FeedbackPolicy

SLOT_STATES = {"MISSING", "PROPOSED", "SELECTED", "CONFLICTED", "STALE"}


class Assembly:
    """One explicitly bound assembly per SQLite file; no external effects or scheduler."""

    def __init__(self, state, spec: AssemblySpec, matcher, policy, *, feedback=None, clock=time.time):
        spec_data = asdict(spec)
        spec_data["slots"] = list(spec_data["slots"])
        self.spec = clone(spec_data)
        self.matcher, self.policy, self.clock = matcher, policy, clock
        self._order = self._validate_spec()
        self.domain_id = spec.domain_id
        self.controller_id = "assembly-" + digest({"assembly_id": spec.assembly_id, "spec": self.spec})
        self._lock = threading.RLock()
        path = Path(state).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contributions(
          result_id TEXT PRIMARY KEY, source TEXT NOT NULL, message_id TEXT NOT NULL,
          bundle_hash TEXT NOT NULL, bundle TEXT NOT NULL, UNIQUE(source,message_id));
        CREATE TABLE IF NOT EXISTS ingestions(
          delivery_id TEXT PRIMARY KEY, bundle_hash TEXT NOT NULL, receipt TEXT NOT NULL,
          feedback_basis TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL,
          at REAL NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(
          revision INTEGER PRIMARY KEY, snapshot_id TEXT UNIQUE NOT NULL, value TEXT NOT NULL);
        ''')
        try:
            with self._transaction():
                stored = self._get("spec")
                if stored is None:
                    self._put("format", 1)
                    self._put("spec", self.spec)
                    self._put("state", {"revision": 0, "status": "ASSEMBLING", "candidates": {},
                        "slots": {s: {"status": "MISSING", "selected_id": None, "reason": "no candidates", "assessments": {}} for s in self._order}})
                    self._put("feedback", self._feedback_value(feedback or FeedbackPolicy()))
                    self._event(0, "CREATED", {"spec_sha256": digest(self.spec)})
                elif stored != self.spec or self._get("format") != 1:
                    raise IdentityConflict("existing assembly specification or storage version differs")
                elif feedback is not None and self._get("feedback") != self._feedback_value(feedback):
                    raise IdentityConflict("change feedback explicitly with set_feedback, not by reopening")
        except BaseException:
            self.db.close()
            raise

    def _validate_spec(self):
        canonical(self.spec)
        for key in ("assembly_id", "version", "domain_id", "catalog_version", "recognizer_version", "matcher_id", "matcher_version", "policy_id", "policy_version"):
            if not isinstance(self.spec[key], str) or not self.spec[key].strip():
                raise SailboatError("nonblank specification identities and versions required")
        if (self.matcher.matcher_id, self.matcher.version) != (self.spec["matcher_id"], self.spec["matcher_version"]):
            raise SailboatError("matcher identity/version mismatch")
        if (self.policy.policy_id, self.policy.version) != (self.spec["policy_id"], self.spec["policy_version"]):
            raise SailboatError("policy identity/version mismatch")
        slots, deps = self.spec["slots"], self.spec["dependencies"]
        if not slots or any(not isinstance(s, str) or not s.strip() for s in slots) or len(set(slots)) != len(slots):
            raise SailboatError("unique nonblank slots required")
        if not isinstance(deps, dict) or set(deps) - set(slots):
            raise SailboatError("unknown dependency slot")
        if any(not isinstance(v, list) or any(d not in slots for d in v) for v in deps.values()):
            raise SailboatError("invalid dependencies")
        order = []
        while len(order) < len(slots):
            ready = [s for s in slots if s not in order and all(d in order for d in deps.get(s, []))]
            if not ready:
                raise SailboatError("cyclic dependencies")
            order.extend(ready)
        return order

    @staticmethod
    def _feedback_value(value):
        data = asdict(value)
        if any(type(v) is not bool for v in data.values()):
            raise SailboatError("feedback settings must be booleans")
        return data

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _get(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, canonical(value)))

    def _event(self, revision, kind, value):
        self.db.execute("INSERT INTO events(revision,at,kind,value) VALUES(?,?,?,?)", (revision, self.clock(), kind, canonical(value)))

    def _context(self, state):
        return clone({"spec": self.spec, "slots": state["slots"]})

    def _reconsider(self, state):
        for slot in self._order:
            all_candidates = [c for c in state["candidates"].values() if c["slot"] == slot]
            active = [c for c in all_candidates if not c["invalidated"]]
            decision = clone(asdict(self.policy.decide(slot, clone(active), self._context(state))))
            ids = {c["id"] for c in active}
            if decision["status"] not in SLOT_STATES or not isinstance(decision["reason"], str):
                raise SailboatError("invalid policy decision")
            assessments = decision["assessments"]
            if set(assessments) != ids or any(not isinstance(a, dict) or a.get("status") not in {"ELIGIBLE", "PENDING", "REJECTED"} or not isinstance(a.get("reason"), str) for a in assessments.values()):
                raise SailboatError("policy must assess every active candidate")
            chosen = decision["selected_id"]
            if decision["status"] == "SELECTED":
                if chosen not in ids or assessments[chosen]["status"] != "ELIGIBLE":
                    raise SailboatError("selection must identify an eligible candidate")
            elif chosen is not None:
                raise SailboatError("unselected slot cannot have a selection")
            if not active and all_candidates:
                decision.update(status="STALE", selected_id=None, reason="all candidates invalidated")
            missing = [s for s in self.spec["dependencies"].get(slot, []) if state["slots"][s]["status"] != "SELECTED"]
            if missing and chosen is not None:
                decision.update(status="PROPOSED", selected_id=None, reason="unsatisfied dependencies: " + ", ".join(missing))
            state["slots"][slot] = decision
        selections = self._selections(state)
        complete = self.policy.complete(clone(selections), self._context(state)) if len(selections) == len(self._order) else False
        if type(complete) is not bool:
            raise SailboatError("completion policy must return boolean")
        state["status"] = "READY" if complete else "ASSEMBLING"

    @staticmethod
    def _selections(state):
        return {s: {"candidate_id": row["selected_id"], "value": state["candidates"][row["selected_id"]]["value"]}
                for s, row in state["slots"].items() if row["status"] == "SELECTED"}

    def accept(self, bundle, delivery_id):
        """ControllerPort receipt completion means recorded ingestion, never solved objective."""
        bundle = clone(bundle)
        fingerprint = digest(bundle)
        if not isinstance(delivery_id, str) or not delivery_id:
            raise SailboatError("delivery identity required")
        with self._transaction():
            existing = self.db.execute("SELECT * FROM ingestions WHERE delivery_id=?", (delivery_id,)).fetchone()
            if existing:
                if existing["bundle_hash"] != fingerprint:
                    raise IdentityConflict("delivery identity reused with different bundle")
                return json.loads(existing["receipt"])
            catalog = bundle["catalog"]
            if bundle.get("protocol") != "sailboat.bundle.v1" or (catalog["domain_id"], catalog["version"], catalog["recognizer_version"]) != (self.domain_id, self.spec["catalog_version"], self.spec["recognizer_version"]):
                raise SailboatError("unsupported bundle protocol/catalog")
            source = bundle["contribution"]
            previous = self.db.execute("SELECT * FROM contributions WHERE result_id=? OR (source=? AND message_id=?)", (bundle["result_id"], source["source"], source["message_id"])).fetchall()
            if previous and any(r["bundle_hash"] != fingerprint for r in previous):
                raise IdentityConflict("contribution identity reused with different sorting result")
            state = self._get("state")
            touched, already = [], []
            if not previous:
                self.db.execute("INSERT INTO contributions VALUES(?,?,?,?,?)", (bundle["result_id"], source["source"], source["message_id"], fingerprint, canonical(bundle)))
                # Never select a supported subset of an unresolved bundle.
                pieces = self.matcher.match(clone(bundle)) if bundle["status"] == "READY" and not bundle["unresolved"] and not any(b["unresolved"] for b in bundle["blocks"]) else []
                block_ids = {b["id"] for b in bundle["blocks"]}
                for piece in pieces:
                    value = asdict(piece)
                    value["block_ids"] = list(value["block_ids"])
                    value = clone(value)
                    slot = value["slot"]
                    if slot not in self._order or not value["block_ids"] or any(i not in block_ids for i in value["block_ids"]):
                        raise SailboatError("piece must identify a declared slot and supporting blocks")
                    if slot not in touched:
                        touched.append(slot)
                        if state["slots"][slot]["status"] == "SELECTED":
                            already.append(slot)
                    ident = "candidate-" + digest({"slot": slot, "value": value["value"]})
                    candidate = state["candidates"].setdefault(ident, {"id": ident, "slot": slot, "value": value["value"], "invalidated": False, "supports": []})
                    support = {"result_id": bundle["result_id"], "block_ids": sorted(set(value["block_ids"])), "bundle": bundle}
                    prior_support = next((s for s in candidate["supports"] if s["result_id"] == bundle["result_id"]), None)
                    if prior_support is not None:
                        prior_support["block_ids"] = sorted(set(prior_support["block_ids"]) | set(support["block_ids"]))
                    else:
                        candidate["supports"].append(support)
                state["revision"] += 1
                self._reconsider(state)
                self._put("state", state)
                self._event(state["revision"], "INGESTED", {"result_id": bundle["result_id"], "slots": touched, "selection_state": state["slots"], "status": state["status"]})
            else:
                # A repeated result under another delivery ID creates no evidence or revision.
                first = self.db.execute("SELECT feedback_basis FROM ingestions WHERE json_extract(receipt,'$.details.result_id')=? ORDER BY rowid LIMIT 1", (bundle["result_id"],)).fetchone()
                if first:
                    basis = json.loads(first[0])
                    touched, already = basis["slots"], basis["already_completed"]
            receipt = {"delivery_id": delivery_id, "status": "COMPLETED", "details": {
                "operation": "assembly_ingestion", "result_id": bundle["result_id"],
                "assembly_id": self.spec["assembly_id"], "revision": state["revision"],
                "duplicate_result": bool(previous), "matched_slots": touched}}
            basis = {"slots": touched, "already_completed": already}
            self.db.execute("INSERT INTO ingestions VALUES(?,?,?,?)", (delivery_id, fingerprint, canonical(receipt), canonical(basis)))
            return clone(receipt)

    def receipt(self, delivery_id):
        with self._lock:
            row = self.db.execute("SELECT receipt FROM ingestions WHERE delivery_id=?", (delivery_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def feedback(self, delivery_id):
        """Contributor projection only. Never forward owner/core receipts to contributors."""
        with self._transaction():
            row = self.db.execute("SELECT feedback_basis FROM ingestions WHERE delivery_id=?", (delivery_id,)).fetchone()
            if not row:
                raise SailboatError("unknown submission")
            settings, state = self._get("feedback"), self._get("state")
            value = {"status": "RECEIVED"}
            if settings["report_already_completed"]:
                value["already_completed"] = json.loads(row[0])["already_completed"]
            if settings["show_selected_pieces"]:
                value["selected_pieces"] = {s: c["value"] for s, c in self._selections(state).items()}
            return clone(value)

    def set_feedback(self, feedback: FeedbackPolicy):
        with self._transaction():
            value = self._feedback_value(feedback)
            self._put("feedback", value)
            self._event(self._get("state")["revision"], "FEEDBACK_CHANGED", value)

    def invalidate(self, candidate_id, *, reason, expected_revision):
        if not isinstance(reason, str) or not reason.strip():
            raise SailboatError("invalidation reason required")
        with self._transaction():
            state = self._get("state")
            if state["revision"] != expected_revision:
                raise IdentityConflict("assembly revision changed")
            if candidate_id not in state["candidates"]:
                raise SailboatError("unknown candidate")
            state["candidates"][candidate_id]["invalidated"] = True
            state["revision"] += 1
            self._reconsider(state)
            self._put("state", state)
            self._event(state["revision"], "INVALIDATED", {"candidate_id": candidate_id, "reason": reason, "selection_state": state["slots"], "status": state["status"]})
            return clone(state)

    def finalize(self, *, expected_revision):
        with self._transaction():
            state = self._get("state")
            if state["revision"] != expected_revision:
                raise IdentityConflict("assembly revision changed")
            if state["status"] != "READY":
                raise SailboatError("assembly is not ready")
            old = self.db.execute("SELECT value FROM snapshots WHERE revision=?", (expected_revision,)).fetchone()
            if old:
                return json.loads(old[0])
            value = {"assembly_id": self.spec["assembly_id"], "revision": expected_revision, "spec": self.spec,
                     "selections": self._selections(state), "candidates": state["candidates"]}
            value["snapshot_id"] = "snapshot-" + digest(value)
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?)", (expected_revision, value["snapshot_id"], canonical(value)))
            self._event(expected_revision, "FINALIZED", {"snapshot_id": value["snapshot_id"]})
            return clone(value)

    def inspect(self):
        """Owner-only full view in the trusted local application, not a remote auth boundary."""
        with self._transaction():
            return {"spec": clone(self.spec), "state": self._get("state"), "feedback": self._get("feedback"),
                    "events": [dict(row) | {"value": json.loads(row["value"])} for row in self.db.execute("SELECT * FROM events ORDER BY sequence")],
                    "snapshots": [json.loads(row[0]) for row in self.db.execute("SELECT value FROM snapshots ORDER BY revision")]}

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
