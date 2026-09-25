"""Conservative sampled health state machine; timestamps are bounds, never invented precision."""

import hashlib
from datetime import datetime


def transition(previous, observation, failure_threshold=2, recovery_threshold=2, max_gap=90):
    state = dict(previous or {})
    entity, at, health = observation["entity"], observation["at"], observation["health"]
    if health not in ("up", "down", "unknown"):
        raise ValueError("Unknown health state")
    emitted, incident = [], state.get("incident")
    if incident:
        incident = dict(incident)
    gap = state.get("at") and (datetime.fromisoformat(at) - datetime.fromisoformat(state["at"])).total_seconds() > max_gap
    if state.get("at") and at <= state["at"]:
        return state, [], None  # stale/duplicate observations cannot rewind a monitor
    def event(kind, summary, severity="info", attributes=None):
        emitted.append({"at": at, "source": observation["source"], "category": "health", "kind": kind,
                        "severity": severity, "summary": summary, "service": entity,
                        "attributes": attributes or {}, "confidence": "observed"})
    if gap or health == "unknown":
        state["gap_since_good"] = True
        state["failures"], state["successes"] = 0, 0
        state.pop("first_bad", None)
        state.pop("first_good", None)
        if incident:
            incident["coverage_gap"] = True
        if gap:
            event("observation_gap", f"{entity}: gap between health observations", "warning")
    invocation = observation.get("invocation_id")
    boot = observation.get("boot_id")
    if boot and state.get("boot_id") and boot != state["boot_id"]:
        event("host_boot_changed", f"{entity}: host boot changed; service restart count is not inferred")
    elif invocation and state.get("invocation_id") and invocation != state["invocation_id"] and boot and boot == state.get("boot_id"):
        event("service_restart", f"{entity}: systemd invocation changed on the same boot", "info",
              {"previous_invocation_id": state["invocation_id"], "invocation_id": invocation,
               "count_lower_bound": 1, "previous_observed_at": state["at"], "observed_at": at})
    if health == "down":
        state["successes"] = 0
        state.pop("first_good", None)
        state["failures"] = state.get("failures", 0) + 1
        state.setdefault("first_bad", at)
        state["last_bad"] = at
        if incident is None and state["failures"] >= failure_threshold:
            first = state["first_bad"]
            incident = {"id": hashlib.sha256(f"{entity}:{first}".encode()).hexdigest()[:24], "entity": entity,
                        "opened_at": first, "confirmed_at": at, "closed_at": None,
                        "start_bounds": {"after": state.get("last_good"), "by": first},
                        "end_bounds": None, "coverage_gap": bool(state.get("gap_since_good")),
                        "left_censored": not bool(state.get("last_good")), "confidence": "sampled",
                        "last_bad": at, "duration_lower_seconds": 0, "duration_upper_seconds": None}
            event("outage_opened", f"{entity}: unhealthy observations confirmed", "critical", {"incident_id": incident["id"]})
        if incident:
            incident["last_bad"] = at
    elif health == "up":
        state["failures"] = 0
        state.pop("first_bad", None)
        state["successes"] = state.get("successes", 0) + 1
        state.setdefault("first_good", at)
        if incident and state["successes"] >= recovery_threshold:
            incident["closed_at"] = state["first_good"]
            incident["recovery_confirmed_at"] = at
            incident["end_bounds"] = {"after": incident["last_bad"], "by": state["first_good"]}
            incident["duration_lower_seconds"] = max(0, (datetime.fromisoformat(incident["last_bad"]) - datetime.fromisoformat(incident["opened_at"])).total_seconds())
            if incident["coverage_gap"]:
                incident["duration_lower_seconds"] = 0
            if incident["start_bounds"]["after"]:
                incident["duration_upper_seconds"] = (datetime.fromisoformat(state["first_good"]) - datetime.fromisoformat(incident["start_bounds"]["after"])).total_seconds()
            event("outage_recovered", f"{entity}: recovery confirmed", "info", {"incident_id": incident["id"]})
        state["last_good"] = at
        state["gap_since_good"] = False
    state.update(at=at, health=health)
    if invocation:
        state["invocation_id"] = invocation
    if boot:
        state["boot_id"] = boot
    state["incident"] = incident if incident and not incident["closed_at"] else None
    return state, emitted, incident
