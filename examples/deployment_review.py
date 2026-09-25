"""Run a synthetic, local-only deployment review walkthrough.

This example writes only temporary HostDelta state. It does not inspect or
change the current host, invoke a deployment, or use the network.
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hostdelta.model import stamp
from hostdelta.store import Store


def synthetic_snapshot(at, enabled):
    return {
        "at": stamp(at),
        "host": "synthetic-host",
        "domains": {
            "packages": {"status": "ok", "data": {}},
            "services": {
                "status": "ok",
                "data": {"example-api.service": {"active": "running", "enabled": enabled}},
            },
            "files": {"status": "ok", "scope": [], "data": {}},
        },
    }


def run_cli(state_dir, *args):
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source, env.get("PYTHONPATH"))))
    subprocess.run(
        [sys.executable, "-m", "hostdelta", "--state-dir", str(state_dir), *args],
        check=True,
        env=env,
    )


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with tempfile.TemporaryDirectory(prefix="hostdelta-deployment-review-") as temporary:
        state_dir = Path(temporary)
        store = Store(state_dir, create=True)
        try:
            store.save(synthetic_snapshot(now - timedelta(minutes=20), enabled=False), "deploy-before")
            store.save(synthetic_snapshot(now - timedelta(minutes=5), enabled=True), "deploy-after")
            store.record({
                "at": stamp(now - timedelta(minutes=10)),
                "category": "agents",
                "kind": "agent_start",
                "severity": "info",
                "source": "local_agent",
                "actor": "demo-deployer",
                "run_id": "synthetic-deploy-001",
                "summary": "synthetic deployment started",
            })
        finally:
            store.close()

        print("SYNTHETIC DEPLOYMENT REVIEW — temporary data only; no host collection or deployment", flush=True)
        print("\n1. Compare the saved before/after service observations:", flush=True)
        run_cli(state_dir, "diff", "deploy-before", "deploy-after", "--full")
        print("\n2. Review the event window that complements the state comparison:", flush=True)
        run_cli(state_dir, "brief", "--archive", "--since", "15m", "--full")
        print("\nSnapshots are observations, not VM restore points. The event is explicitly synthetic and self-reported.")


if __name__ == "__main__":
    main()
