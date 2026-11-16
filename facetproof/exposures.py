import json
from .camera import mapping_active
from .errors import require
from .mapping import analyze
from .planner import validate_request
from .values import identity, key, digest, canonical, now


class Exposures:
    def __init__(self, db, resources=None):
        self.db = db
        if resources is None:
            from .resources import ResourceBudget
            resources = ResourceBudget(db)
        self.resources = resources

    def _get(self, conn, exposure_id):
        row = conn.execute("SELECT * FROM exposures WHERE id=?", (exposure_id,)).fetchone()
        require(row is not None, "not_found", "Exposure not found", 404)
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["allocation"] = self.resources.get(conn, exposure_id)
        return result

    def get(self, exposure_id):
        with self.db.unit_of_work() as conn:
            return self._get(conn, exposure_id)

    def submit(self, seal_id, data):
        request = validate_request(data)
        dedup_key = key(data.get("key"), "key")
        request_hash = digest({"seal_id": seal_id, "request": request})
        with self.db.unit_of_work() as conn:
            seal = self.db.get(conn, seal_id, "seal")
            lens = seal["payload"]["lens"]
            if mapping_active(lens):
                # Reject impossible calibrations before reserving capacity;
                # the worker re-derives the same mapping deterministically.
                analyze(lens, request["width"], request["height"], request["mapping_tolerance"])
            old = conn.execute("SELECT id,request_hash FROM exposures WHERE dedup_key=?", (dedup_key,)).fetchone()
            if old:
                require(old["request_hash"] == request_hash, "key_conflict", "Key already names a different exposure", 409)
                return self._get(conn, old["id"])
            exposure_id = identity("exposure")
            self.resources.reserve(conn, exposure_id, request)
            stamp = now()
            conn.execute("INSERT INTO exposures(id,seal_id,state,request,request_hash,dedup_key,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                         (exposure_id, seal_id, "queued", canonical(request), request_hash, dedup_key, stamp, stamp))
            self.db.event(conn, exposure_id, "queued", {"seal_id": seal_id})
            return self._get(conn, exposure_id)

    def claim(self, lease_seconds=120):
        with self.db.unit_of_work() as conn:
            row = conn.execute("""SELECT e.id FROM exposures e JOIN sample_allocations a ON a.exposure_id=e.id
                WHERE e.state='queued' AND a.active=1 ORDER BY e.created,e.id LIMIT 1""").fetchone()
            if not row:
                return None
            obj = self._get(conn, row['id'])
            self.resources.require_allocation(conn, obj)
            token = identity("lease")
            conn.execute("UPDATE exposures SET state='running',attempt=attempt+1,token=?,lease_until=?,updated=? WHERE id=?",
                         (token, now()+lease_seconds, now(), row["id"]))
            self.db.event(conn, row["id"], "claimed", {"token": token})
            return self._get(conn, row["id"])

    def heartbeat(self, exposure_id, token, lease_seconds=120):
        with self.db.unit_of_work() as conn:
            obj = self._get(conn, exposure_id)
            require(obj["state"] == "running" and obj["token"] == token and obj["lease_until"] > now(),
                    "lease_lost", "Exposure lease no longer owned", 409)
            require(not obj["cancel_requested"], "cancelled", "Exposure cancellation requested", 409)
            self.resources.require_allocation(conn, obj)
            conn.execute("UPDATE exposures SET lease_until=?,updated=? WHERE id=? AND token=?",
                         (now()+lease_seconds, now(), exposure_id, token))
            return obj

    def cancel(self, exposure_id):
        with self.db.unit_of_work() as conn:
            obj = self._get(conn, exposure_id)
            require(obj["state"] in {"queued", "running", "cancelled"}, "state", "Completed exposure cannot cancel", 409)
            state = "cancelled" if obj["state"] in {"queued", "cancelled"} else "running"
            conn.execute("UPDATE exposures SET state=?,cancel_requested=1,updated=? WHERE id=?", (state, now(), exposure_id))
            self.db.event(conn, exposure_id, "cancel_requested", {"state": state})
            if state == 'cancelled':
                self.resources.release(conn, exposure_id, 'cancelled')
            return self._get(conn, exposure_id)

    def retry(self, exposure_id):
        with self.db.unit_of_work() as conn:
            obj = self._get(conn, exposure_id)
            require(obj["state"] == "failed", "state", "Only failed exposures may retry", 409)
            self.resources.reserve(conn, exposure_id, obj['request'])
            conn.execute("UPDATE exposures SET state='queued',token=NULL,lease_until=NULL,error=NULL,cancel_requested=0,updated=? WHERE id=?",
                         (now(), exposure_id))
            self.db.event(conn, exposure_id, "retried", {"attempt": obj["attempt"]})
            return self._get(conn, exposure_id)

    def fail(self, exposure_id, token, error):
        with self.db.unit_of_work() as conn:
            obj = self._get(conn, exposure_id)
            if obj["state"] != "running" or obj["token"] != token or obj["lease_until"] <= now():
                return obj
            state = "cancelled" if obj["cancel_requested"] else "failed"
            conn.execute("UPDATE exposures SET state=?,error=?,token=NULL,lease_until=NULL,updated=? WHERE id=? AND token=?",
                         (state, error, now(), exposure_id, token))
            self.db.event(conn, exposure_id, state, {"error": error})
            self.resources.release(conn, exposure_id, state)
            return self._get(conn, exposure_id)

    def recover(self):
        recovered = []
        with self.db.unit_of_work() as conn:
            rows = conn.execute("SELECT id,cancel_requested FROM exposures WHERE state='running' AND lease_until<=?", (now(),)).fetchall()
            for row in rows:
                state = "cancelled" if row["cancel_requested"] else "queued"
                conn.execute("UPDATE exposures SET state=?,token=NULL,lease_until=NULL,updated=? WHERE id=?", (state, now(), row["id"]))
                self.db.event(conn, row["id"], "recovered", {"state": state})
                if state == 'cancelled':
                    self.resources.release(conn, row['id'], state)
                recovered.append(row["id"])
        return {"recovered": recovered}

    def list(self):
        with self.db.unit_of_work() as conn:
            rows = conn.execute("SELECT id FROM exposures ORDER BY created,id").fetchall()
            return [self._get(conn, row["id"]) for row in rows]
