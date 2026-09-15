"""Dev utility: run the replay scenarios the Replay Engine is specified against.

    python -m scripts.replay_scenarios

Requires the target app running on 127.0.0.1:5050. Set ENGINE_HEADLESS=1 for no window.

Note what these use: hand-written fixture artifacts from `fixtures/corebank/`, including
`operator_login` — authentication is an ordinary capability here, replayed as a prerequisite,
not something the engine knows how to do.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from engine.escalation.service import EscalationService
from engine.replay.engine import ReplayEngine
from engine.reporting.report_generator import ReportGenerator
from engine.schema.artifact import Artifact
from engine.storage.artifact_store import ArtifactStore
from engine.surface.playwright_surface import PlaywrightSurface

FIXTURES = Path("fixtures/corebank")
ENTRY = "http://127.0.0.1:5050"

CREDENTIALS = {
    "username": os.environ.get("COREBANK_USERNAME", "operator1"),
    "password": os.environ.get("COREBANK_PASSWORD", "pass123"),
}

SCENARIOS = [
    ("check_savings_balance", {"member_id": "10001"}, "success"),
    ("check_savings_balance", {"member_id": "99999"}, "business_outcome"),
    ("check_savings_balance", {"member_id": "10005"}, "business_outcome"),
    ("transfer_funds", {"member_id": "10003", "amount": "50.00", "target_account": "778812345"},
     "recoverable"),
    ("transfer_funds", {"member_id": "10001", "amount": "2000000", "target_account": "778812345"},
     "hard_failure"),
    ("transfer_funds", {"member_id": "10001", "amount": "25.00", "target_account": "778812345"},
     "success"),
]


class FixtureStore(ArtifactStore):
    """Reads fixtures instead of the real Artifact Store, so a dev sweep never depends on
    what happens to be recorded, and never writes into it."""

    def __init__(self) -> None:
        self.root = FIXTURES

    def save(self, artifact):  # pragma: no cover - fixtures are read-only
        raise RuntimeError("fixtures are read-only")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    store = FixtureStore()
    failures = 0

    with PlaywrightSurface(base_url=ENTRY) as surface:
        engine = ReplayEngine(
            surface,
            # Non-interactive: the intervention record is still written and reported, it
            # just does not block a terminal nobody is watching during a scripted sweep.
            escalation=EscalationService(interactive=False, surface=surface),
            report_generator=ReportGenerator(),
            store=store,
        )
        for capability_id, params, expected in SCENARIOS:
            artifact = store.load(capability_id)
            result = engine.run(artifact, {**params, **CREDENTIALS})
            ok = result.status == expected
            failures += 0 if ok else 1
            print(
                f"[{'PASS' if ok else 'FAIL'}] {capability_id} {params} "
                f"-> {result.status} ({result.outcome_code or '-'}); expected {expected}"
            )
            if result.outputs:
                print(f"         outputs: {result.outputs}")
            if result.message:
                print(f"         message: {result.message}")
            print(f"         report:  evidence/replay_runs/{result.run_id}/report.md")

    print("\nAll scenarios passed." if not failures else f"\n{failures} scenario(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
