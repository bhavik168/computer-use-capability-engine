"""Dev utility: exercise every branch of the replay result contract, end to end.

    python -m scripts.replay_scenarios

Requires the target app running on 127.0.0.1:5050. Set ENGINE_HEADLESS=1 for no window.

This is the closest thing the project has to an integration test for the thing that matters
most about replay: not "did it click the button" but "did it tell the caller the truth about
what happened". The four statuses are asserted against real runs on a real browser against a
real application — `success`, `business_outcome`, `recoverable`, `hard_failure`.

It runs hermetically. The artifacts are copied into a temporary store and the Knowledge Base
is a temporary directory, because two of these scenarios *teach* the system something and
one of them bumps an artifact's version. A dev sweep must not rewrite the committed
capability catalogue as a side effect of being run, and it must start from the same cold KB
every time or the "first encounter is a hard failure" scenarios would pass only once.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from engine.escalation.service import EscalationService
from engine.knowledge_base.service import KnowledgeBaseService
from engine.knowledge_base.store import KnowledgeBaseStore
from engine.learning.outcome_learner import OutcomeLearner
from engine.replay.engine import ReplayEngine
from engine.reporting.report_generator import ReportGenerator
from engine.storage.artifact_store import ArtifactStore
from engine.surface.playwright_surface import PlaywrightSurface

ARTIFACTS = Path("data/artifacts")
ENTRY = "http://127.0.0.1:5050"
APP_ID = "127.0.0.1_5050"

BALANCE = "lookup_member_and_get_savings_balance"

CREDENTIALS = {
    "username": os.environ.get("COREBANK_USERNAME", "operator1"),
    "password": os.environ.get("COREBANK_PASSWORD", "pass123"),
}


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    with tempfile.TemporaryDirectory(prefix="replay_scenarios_") as tmp:
        sandbox = Path(tmp)
        shutil.copytree(ARTIFACTS, sandbox / "artifacts")
        store = ArtifactStore(sandbox / "artifacts")
        kb = KnowledgeBaseService(KnowledgeBaseStore(sandbox / "kb"))
        learner = OutcomeLearner(store=store, knowledge_base=kb)
        failures = 0

        def check(label, capability_id, params, expected):
            """Run one scenario in its own browser.

            A fresh context per scenario on purpose, because that is what production looks
            like: an agent invokes a capability, it establishes its own session, it ends.
            Sharing one browser across scenarios would replay `operator_login` into an app
            that is already signed in, where the Username field it was recorded against does
            not exist — a property of this harness, not of the capability.
            """
            nonlocal failures
            with PlaywrightSurface(base_url=ENTRY) as surface:
                engine = ReplayEngine(
                    surface,
                    # Non-interactive: the intervention record is still written and
                    # reported, it just does not block a terminal nobody is watching.
                    escalation=EscalationService(interactive=False, surface=surface),
                    report_generator=ReportGenerator(),
                    store=store,
                    knowledge_base=kb,
                )
                result = engine.run(store.load(capability_id), {**params, **CREDENTIALS})
                ok = result.status == expected
                failures += 0 if ok else 1
                print(f"[{'PASS' if ok else 'FAIL'}] {label}")
                print(f"         {capability_id} {params}")
                print(f"         -> {result.status} ({result.outcome_code or '-'}); "
                      f"expected {expected}")
                if result.outputs:
                    print(f"         outputs: {result.outputs}")
                if result.message:
                    print(f"         message: {result.message}")
                print(f"         report:  evidence/replay_runs/{result.run_id}/report.md")
                return result

        # 1. The happy path, and the only one that returns data to the caller.
        check("a recorded capability replays and returns its declared output",
              BALANCE, {"member_id": "10001"}, "success")

        # 2. An application state nothing has ever named. Reporting this as a hard
        #    failure is the design working: the system has no basis for deciding that an
        #    unfamiliar red banner means "routine" rather than "the database is down".
        check("an unknown refusal is a hard failure on first encounter",
              BALANCE, {"member_id": "99999"}, "hard_failure")

        # 3. A human names it once, for the application rather than the capability.
        learner.learn(
            app_id=APP_ID, code="member_not_found", outcome_type="business_outcome",
            detector={"text_contains": "No matching member found"},
            message="No member exists with the supplied id.",
            capability_id=BALANCE,
        )
        check("the same input is a clean business outcome once it has been named",
              BALANCE, {"member_id": "99999"}, "business_outcome")

        # 4. A different refusal is still unknown — learning one outcome does not
        #    silently teach the system every other way the app can say no.
        check("a different unknown refusal is still a hard failure",
              BALANCE, {"member_id": "10005"}, "hard_failure")

        learner.learn(
            app_id=APP_ID, code="permission_denied", outcome_type="business_outcome",
            detector={"text_contains": "You are not authorized to view this record"},
            message="The member is restricted and cannot be serviced by this operator.",
            capability_id=BALANCE,
        )
        check("a restricted member is a business outcome once named",
              BALANCE, {"member_id": "10005"}, "business_outcome")

        # 5. Recoverable, not business: the caller learns nothing about the member, and
        #    the same call may well succeed on a retry after signing on again. Requires
        #    the target app started with SESSION_TIMEOUT_SECONDS=1; see README.
        learner.learn(
            app_id=APP_ID, code="session_expired", outcome_type="recoverable",
            detector={"url_contains": "/session-expired"},
            message="The operator session expired mid-flow.",
            recovery="Replay the operator_login prerequisite and retry.",
            capability_id=BALANCE,
        )
        if os.environ.get("SESSION_TIMEOUT_SECONDS") == "1":
            check("an expired session is recoverable, not a business outcome",
                  BALANCE, {"member_id": "10001"}, "recoverable")
        else:
            print("[SKIP] session-expiry scenario — restart the target app with "
                  "SESSION_TIMEOUT_SECONDS=1 and set it here too to run it")

        print("\nAll scenarios passed." if not failures else f"\n{failures} scenario(s) failed.")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
