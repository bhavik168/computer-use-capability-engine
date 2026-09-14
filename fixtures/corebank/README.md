# Target fixtures — hand-crafted, NOT discovery output

These live outside `engine/` on purpose. **The engine contains no knowledge of any
application**; everything target-specific is here or in `profiles/corebank.yaml`. Point the
engine at a different app and nothing in `engine/` changes — these files simply no longer
apply, and you write new ones (or none).

Both JSON files are **hand-written test data**, authored against this target app's routes and
markup so the Replay Engine could be built and proven before the Discovery Engine existed.
That sequencing was deliberate — it isolates "does deterministic replay work" from "does the
LLM loop work", which are two very different things to debug.

They are **not** evidence of an LLM-driven run and must never be read as such. Their
`provenance.created_from_run` says so explicitly (`fixture_handcrafted_*`). Genuine discovery
output is written by the Recorder to `data/artifacts/`, with its trace and screenshots under
`evidence/discovery_runs/`.

| File | Shape it exercises |
|---|---|
| `check_savings_balance.json` | Safe, read-only, one output, extraction from inside an iframe. |
| `transfer_funds.json` | Multi-step, three input params, one `risky` step (Confirm Transfer). |

`transfer_funds.json` deliberately has **no** `known_outcomes` entry for a transfer amount
over 1,000,000. That case is a hard failure precisely because nothing in the artifact was
recorded to expect it; adding a detector would turn it into a handled business outcome and
destroy what it is there to test.
