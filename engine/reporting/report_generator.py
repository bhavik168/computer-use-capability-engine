"""Human-readable evidence for every run, whatever its outcome.

The Recorder fires only on success, because only a success is worth freezing. The Report
Generator fires on *every* termination — success, business outcome, escalation, hard
failure — because a person reviewing a run needs the narrated, screenshot-backed account
precisely when something went wrong.

Discovery and replay reports use one format. The only difference is the rationale line:
discovery has a model's stated reason for each step, and replay has none to show, because
there was no model in the loop. That asymmetry is the point rather than an inconsistency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from engine.reporting.redaction import redact_screenshot

log = logging.getLogger(__name__)

EVIDENCE_ROOT = Path("evidence")


@dataclass
class NormalizedStep:
    """Discovery trace steps and replay step logs, reduced to what a report renders."""

    index: int
    title: str
    description: str
    rationale: str | None
    locator_note: str | None
    verified: bool
    url: str | None
    screenshot: bytes | None


class ReportGenerator:
    def __init__(self, root: Path | str = EVIDENCE_ROOT) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ entry points

    def generate_for_discovery(self, trace) -> Path:
        steps = [
            NormalizedStep(
                index=i + 1,
                title=f"{step.action}: {step.element_name or step.url_value or ''}".strip(" :"),
                description=step.description or step.action,
                rationale=step.rationale,
                locator_note=step.locator_value(),
                verified=step.verified,
                url=step.url_after or step.url_before,
                screenshot=step.screenshot,
            )
            for i, step in enumerate(trace.steps)
        ]
        outcome = {
            "heading": trace.goal,
            "goal": trace.goal,
            "target": f"{trace.app_id} (web)",
            "started": trace.started_at,
            "duration": trace.duration_seconds,
            "outcome": _discovery_outcome(trace),
            "result": _discovery_result(trace),
        }
        return self.generate(trace.run_id, "discovery", steps, outcome, None)

    def generate_for_replay(self, result, artifact=None) -> Path:
        steps = [
            NormalizedStep(
                index=i + 1,
                title=f"{log_entry.action}: {log_entry.step_id}",
                description=log_entry.description,
                rationale=None,  # replay has no LLM rationale to show, by construction
                locator_note=log_entry.locator_strategy,
                verified=log_entry.verified,
                url=log_entry.url,
                screenshot=log_entry.screenshot,
            )
            for i, log_entry in enumerate(result.steps_executed)
        ]
        outcome = {
            "heading": result.capability_id or "replay",
            "goal": (artifact.description if artifact else result.capability_id) or "",
            "target": (
                f"{artifact.target_app.app_id} ({artifact.target_app.surface_type})"
                if artifact
                else "unknown"
            ),
            "started": result.started_at,
            "duration": result.duration_seconds,
            "outcome": _replay_outcome(result),
            "result": _replay_result(result),
        }
        return self.generate(result.run_id or "replay_run", "replay", steps, outcome, None)

    # ------------------------------------------------------------------ rendering

    def generate(
        self,
        run_id: str,
        kind: str,
        steps: list[NormalizedStep],
        outcome_summary: dict,
        out_dir: Path | None = None,
    ) -> Path:
        directory = Path(out_dir) if out_dir else self.root / f"{kind}_runs" / run_id
        shots = directory / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Run report: {outcome_summary.get('heading', run_id)}",
            "",
            f"**Run id:** `{run_id}`  ",
            f"**Goal:** {outcome_summary.get('goal', '')}  ",
            f"**Target:** {outcome_summary.get('target', '')}  ",
            f"**Started:** {outcome_summary.get('started', '')} · "
            f"**Duration:** {outcome_summary.get('duration', '?')}s · "
            f"**Outcome:** {outcome_summary.get('outcome', '')}",
            "",
            "## Steps",
            "",
        ]

        if not steps:
            lines.append("_No steps were executed._\n")

        for step in steps:
            name = f"step{step.index}.png"
            if step.screenshot:
                # Every screenshot write routes through the redaction boundary — no
                # exceptions, even while it is a no-op.
                (shots / name).write_bytes(redact_screenshot(step.screenshot))

            lines.append(f"### {step.index}. {step.title}")
            if step.screenshot:
                lines.append(f"![screenshot](screenshots/{name})")
            lines.append(step.description + ".")
            if step.rationale:
                lines.append(f"*Rationale: {step.rationale}*")
            locator = step.locator_note or "n/a"
            lines.append(
                f"Locator: {locator} · Verified: {'✓' if step.verified else '✗'}"
                + (f" · URL: `{step.url}`" if step.url else "")
            )
            lines.append("")

        lines.append("## Result")
        lines.append("")
        lines.append(outcome_summary.get("result", ""))
        lines.append("")

        report = directory / "report.md"
        report.write_text("\n".join(lines))
        log.info("wrote %s report to %s", kind, report)
        return report


# ---------------------------------------------------------------------- summaries

_STATUS_ICONS = {
    "success": "✅ Success",
    "business_outcome": "⚠️ Business outcome",
    "recoverable": "🔁 Recoverable",
    "hard_failure": "❌ Hard failure",
}


def _replay_outcome(result) -> str:
    label = _STATUS_ICONS.get(result.status, result.status)
    return f"{label}" + (f": `{result.outcome_code}`" if result.outcome_code else "")


def _replay_result(result) -> str:
    if result.status == "success":
        outputs = "\n".join(f"- `{k}` = `{v}`" for k, v in result.outputs.items())
        return "The capability completed and its checkpoint held.\n\n" + (
            outputs or "_No outputs declared._"
        )
    body = [f"**Status:** {result.status}", f"**Outcome code:** `{result.outcome_code}`"]
    if result.failed_at_step:
        body.append(f"**Failed at step:** `{result.failed_at_step}`")
    if result.message:
        body.append(f"\n{result.message}")
    return "\n\n".join(body)


def _discovery_outcome(trace) -> str:
    if trace.status == "completed":
        return "✅ Goal reached"
    if trace.status == "paused_for_escalation":
        return "⏸ Paused for escalation"
    if trace.status == "blocked_by_policy":
        return "⛔ Blocked by policy"
    return f"❌ {trace.status}"


def _discovery_result(trace) -> str:
    parts = [
        f"**Status:** {trace.status}",
        f"**LLM calls:** {trace.llm_calls} across {len(trace.steps)} step(s)",
        f"**Final URL:** `{trace.final_url}`",
    ]
    if trace.goal_evidence:
        parts.append(f"**Evidence the model cited:** {trace.goal_evidence}")
    if trace.stop_reason:
        parts.append(f"**Stop reason:** {trace.stop_reason}")
    return "\n\n".join(parts)
