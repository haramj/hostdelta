"""Transactional ingestion: events, source offsets, and health transitions commit together."""

from datetime import timedelta
import hashlib
import json

from .incidents import transition
from .readiness import assess
from .model import stamp, utcnow
from .telemetry import redact


class Archive:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def checkpoint(self, source):
        row = self.db.execute("SELECT payload FROM checkpoints WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _insert(self, item, observed):
        item = redact(item)
        identity = item.get("event_id") or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        item["event_id"], item["ref"] = identity, "event:" + identity
        added = self.db.execute("INSERT OR IGNORE INTO archive_events VALUES (?,?,?,?,?,?,?,?,?)",
                               (identity, item["at"], observed, item["source"], item.get("service"), item["severity"], item.get("trace_id"), item["kind"], json.dumps(item))).rowcount
        if added and item.get("kind") == "http_request":
            request = item["attributes"]
            self.db.execute("INSERT INTO archive_http VALUES (?,?,?,?,?,?,?)", (identity, item["at"], request["method"], request["path"], request["ip"], request["status"], int(request["slow"])))
        return added

    def ingest(self, source, events, checkpoint=None, status="ok", warnings=(), observations=(), config=None):
        observed = stamp(utcnow())
        count = 0
        cfg = config or {}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for event in events:
                count += self._insert(event, observed)
            for observation in observations:
                row = self.db.execute("SELECT payload FROM monitors WHERE entity=?", (observation["entity"],)).fetchone()
                prior = json.loads(row[0]) if row else None
                state, emitted, incident = transition(prior, observation, cfg.get("failure_threshold", 2),
                                                      cfg.get("recovery_threshold", 2), cfg.get("interval_seconds", 30) * 3)
                self.db.execute("INSERT OR REPLACE INTO monitors VALUES (?,?)", (observation["entity"], json.dumps(state)))
                for event in emitted:
                    count += self._insert(event, observed)
                if incident:
                    self.db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?)", (incident["id"], incident["entity"], incident["opened_at"], incident["closed_at"], json.dumps(incident)))
            if checkpoint is not None:
                self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (source, json.dumps(checkpoint)))
            payload = {"inserted": count, "warnings": redact(list(warnings))}
            self.db.execute("INSERT INTO source_runs(source,at,status,payload) VALUES (?,?,?,?)", (source, observed, status, json.dumps(payload)))
        return count

    def query(self, since, until, service=None, severity=None, trace_id=None, limit=1000, exclude_http=False):
        conditions, values = ["at>?", "at<=?"], [since, until]
        if exclude_http:
            conditions.append("kind!='http_request'")
        for field, value in (("service", service), ("severity", severity), ("trace_id", trace_id)):
            if value:
                conditions.append(field + "=?")
                values.append(value)
        rows = self.db.execute("SELECT payload FROM archive_events WHERE " + " AND ".join(conditions) + " ORDER BY at,event_id LIMIT ?", (*values, limit + 1)).fetchall()  # nosec B608
        return [json.loads(row[0]) for row in rows[:limit]], len(rows) > limit

    def incidents(self, since, until):
        rows = self.db.execute("SELECT payload FROM incidents WHERE opened_at<=? AND (closed_at IS NULL OR closed_at>?) ORDER BY opened_at DESC LIMIT 1000", (until, since)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def http_summary(self, since, until):
        params = (since, until)
        row = self.db.execute("SELECT COUNT(*),COALESCE(SUM(status>=500),0),COALESCE(SUM(slow),0),MIN(at),MAX(at) FROM archive_http WHERE at>? AND at<=?", params).fetchone()
        codes = self.db.execute("SELECT status,COUNT(*) FROM archive_http WHERE at>? AND at<=? GROUP BY status", params).fetchall()
        paths = self.db.execute("SELECT method || ' ' || path,COUNT(*) n FROM archive_http WHERE at>? AND at<=? GROUP BY method,path ORDER BY n DESC,method,path LIMIT 10", params).fetchall()
        clients = self.db.execute("SELECT ip,COUNT(*) n FROM archive_http WHERE at>? AND at<=? GROUP BY ip ORDER BY n DESC,ip LIMIT 10", params).fetchall()
        return {"status": "ok", "total": row[0], "errors_5xx": row[1], "slow_requests": row[2], "first_request": row[3], "last_request": row[4],
                "status_codes": {str(r[0]): r[1] for r in codes}, "top_paths": [list(r) for r in paths], "top_clients": [list(r) for r in clients],
                "files_read": None, "source": "archive", "warnings": ["Counts cover durably collected HTTP records only; inspect source coverage for gaps."]}

    def coverage(self, since, until):
        rows = self.db.execute("SELECT source,status,COUNT(*) AS runs FROM source_runs WHERE at>? AND at<=? GROUP BY source,status", (since, until)).fetchall()
        return [dict(row) for row in rows]

    def prune(self, days, dry_run=False):
        cutoff = stamp(utcnow() - timedelta(days=days))
        predicates = {
            "archive_events": ("observed_at<?", (cutoff,)),
            "source_runs": ("at<?", (cutoff,)),
            "events": ("at<?", (cutoff,)),
            "sessions": ("at<? AND id NOT IN (SELECT id FROM sessions ORDER BY id DESC LIMIT 1)", (cutoff,)),
            "incidents": ("closed_at IS NOT NULL AND closed_at<?", (cutoff,)),
            "snapshots": ("label IS NULL AND at<? AND id NOT IN (SELECT id FROM snapshots WHERE at<=? ORDER BY at DESC,id DESC LIMIT 1) AND id NOT IN (SELECT id FROM snapshots ORDER BY at DESC,id DESC LIMIT 1)", (cutoff, cutoff)),
        }
        counts = {}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for table, (predicate, params) in predicates.items():
                counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}", params).fetchone()[0]  # nosec B608
                if not dry_run:
                    self.db.execute(f"DELETE FROM {table} WHERE {predicate}", params)  # nosec B608
            if not dry_run:
                floor = max(cutoff, self.store.setting("retention_floor", cutoff))
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('retention_floor',?)", (json.dumps(floor),))
        return {"cutoff": cutoff, "dry_run": dry_run, "rows": counts, "preserved": "labeled snapshots, one boundary baseline, latest snapshot/session, open incidents, ingestion and consumer cursors"}

    def status(self):
        rows = self.db.execute("SELECT r.* FROM source_runs r JOIN (SELECT source,MAX(id) id FROM source_runs GROUP BY source) latest ON r.id=latest.id ORDER BY r.source").fetchall()
        result = {"sources": [{"source": r["source"], "at": r["at"], "status": r["status"], **json.loads(r["payload"])} for r in rows],
                "events": self.db.execute("SELECT COUNT(*) FROM archive_events").fetchone()[0],
                "open_incidents": self.db.execute("SELECT COUNT(*) FROM incidents WHERE closed_at IS NULL").fetchone()[0],
                "daemon": self.store.setting("daemon", {}), "retention_floor": self.store.setting("retention_floor")}

        result.update(assess(result["sources"], result["daemon"], self.store.setting("collector_config", {}), utcnow()))
        return result
