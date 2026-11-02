"""A blank box: explicit adapters, immutable sorting, recoverable deliveries."""
import json
import time
import uuid
from .contracts import Contribution, DomainPackage, Resolver, ControllerPort, IdentityConflict, SailboatError, canonical, clone, digest
from .storage import Store
from . import validation


class Box:
    def __init__(self, state, *, clock=time.time):
        self.store = Store(state)
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.store.close()

    def sort(self, contribution: Contribution, domain: DomainPackage, *, resolver: Resolver | None = None,
             context: dict | None = None) -> dict:
        data = validation.contribution(contribution)
        spec = validation.catalog(domain.catalog)
        fingerprint = digest({"domain_id": spec["domain_id"], "contribution": data})
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM results WHERE source=? AND message_id=?", (data["source"], data["message_id"])).fetchone()
            if old:
                if old["input_hash"] != fingerprint:
                    raise IdentityConflict("source/message identity reused with changed content or domain")
                return json.loads(old["value"])
            parsed = validation.recognition(domain.recognize(Contribution(**clone(data))), data["text"], spec)
            result = {"result_id": "sort-" + uuid.uuid4().hex, "protocol": "sailboat.bundle.v1",
                      "received_at": self.clock(), "contribution": data, "catalog": spec,
                      "catalog_sha256": digest(spec), "source_sha256": digest(data), **parsed,
                      "findings": [], "resolver": None}
            if resolver is not None:
                if resolver.domain_id != spec["domain_id"]:
                    raise SailboatError("resolver domain mismatch")
                result["resolver"] = {"id": resolver.resolver_id, "version": resolver.version}
                try:
                    facts = resolver.resolve(clone(result), clone(context or {}))
                    if not isinstance(facts, list) or any(not isinstance(f, dict) for f in facts):
                        raise SailboatError("resolver must return a list of finding objects")
                    result["findings"] = clone(facts)
                except Exception as exc:
                    result["findings"] = [{"status": "UNAVAILABLE", "reason": type(exc).__name__}]
            db.execute("INSERT INTO results VALUES(?,?,?,?,?)", (result["result_id"], data["source"], data["message_id"], fingerprint, canonical(result)))
            return clone(result)

    def result(self, result_id: str) -> dict:
        with self.store.lock:
            row = self.store.db.execute("SELECT value FROM results WHERE id=?", (result_id,)).fetchone()
            if not row:
                raise SailboatError("unknown sorting result")
            return json.loads(row[0])

    def _event(self, db, delivery_id, event, value):
        db.execute("INSERT INTO history(delivery_id,at,event,value) VALUES(?,?,?,?)", (delivery_id, self.clock(), event, canonical(value)))

    def _record_receipt(self, delivery_id, value):
        value = validation.controller_receipt(value, delivery_id)
        with self.store.transaction() as db:
            old = db.execute("SELECT receipt FROM deliveries WHERE id=?", (delivery_id,)).fetchone()[0]
            if old and json.loads(old)["status"] in validation.TERMINAL and canonical(value) != old:
                raise SailboatError("terminal controller receipt changed")
            if old != canonical(value):
                self._event(db, delivery_id, "CONTROLLER_RECEIPT", value)
            db.execute("UPDATE deliveries SET status='ACKNOWLEDGED', receipt=?,error=NULL WHERE id=?", (canonical(value), delivery_id))

    def _unknown(self, delivery_id, reason):
        with self.store.transaction() as db:
            db.execute("UPDATE deliveries SET status='UNKNOWN',error=? WHERE id=?", (reason, delivery_id))
            self._event(db, delivery_id, "OUTCOME_UNKNOWN", {"reason": reason})

    def _recover(self, delivery_id, controller):
        row = self.receipt(delivery_id)
        if row["controller_id"] != controller.controller_id:
            raise SailboatError("recovery controller identity mismatch")
        if self.result(row["result_id"])["catalog"]["domain_id"] != controller.domain_id:
            raise SailboatError("recovery controller domain mismatch")
        if row["controller_receipt"] and row["controller_receipt"]["status"] in validation.TERMINAL:
            return row
        with self.store.transaction() as db:
            self._event(db, delivery_id, "RECOVERY_REQUESTED", {})
        try:
            value = controller.receipt(delivery_id)
            if value is None:
                self._unknown(delivery_id, "controller has no confirmed receipt; no automatic resubmission")
            else:
                self._record_receipt(delivery_id, value)
        except Exception as exc:
            self._unknown(delivery_id, type(exc).__name__ + ": " + str(exc)[:200])
        return self.receipt(delivery_id)

    def deliver(self, result_id: str, controller: ControllerPort) -> dict:
        with self.store.delivery_lock():
            result = self.result(result_id)
            if controller.domain_id != result["catalog"]["domain_id"] or not isinstance(controller.controller_id, str) or not controller.controller_id:
                raise SailboatError("controller identity/domain mismatch")
            with self.store.transaction() as db:
                old = db.execute("SELECT id FROM deliveries WHERE result_id=? AND controller_id=?", (result_id, controller.controller_id)).fetchone()
                if old:
                    delivery_id = old[0]
                else:
                    delivery_id = "delivery-" + uuid.uuid4().hex
                    db.execute("INSERT INTO deliveries VALUES(?,?,?,'PREPARED',NULL,NULL)", (delivery_id, result_id, controller.controller_id))
                    self._event(db, delivery_id, "PREPARED", {"result_id": result_id, "controller_id": controller.controller_id})
            if old:
                return self._recover(delivery_id, controller)
            with self.store.transaction() as db:
                db.execute("UPDATE deliveries SET status='IN_FLIGHT' WHERE id=?", (delivery_id,))
                self._event(db, delivery_id, "ACCEPT_ATTEMPT", {})
            try:
                value = controller.accept(clone(result), delivery_id)
                self._record_receipt(delivery_id, value)
            except Exception as exc:
                self._unknown(delivery_id, type(exc).__name__ + ": " + str(exc)[:200])
            return self.receipt(delivery_id)

    def receipt(self, delivery_id: str, *, controller: ControllerPort | None = None, recover=False) -> dict:
        if recover:
            if controller is None:
                raise SailboatError("explicit controller required for recovery")
            with self.store.delivery_lock():
                return self._recover(delivery_id, controller)
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise SailboatError("unknown delivery")
            events = self.store.db.execute("SELECT sequence,at,event,value FROM history WHERE delivery_id=? ORDER BY sequence", (delivery_id,)).fetchall()
            return {"delivery_id": row["id"], "result_id": row["result_id"], "controller_id": row["controller_id"],
                    "delivery_status": row["status"], "controller_receipt": json.loads(row["receipt"]) if row["receipt"] else None,
                    "error": row["error"], "history": [dict(e) | {"value": json.loads(e["value"])} for e in events]}
