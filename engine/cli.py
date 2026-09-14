"""Capability API / CLI — the single entry point an agent (or an operator) drives.

Pure glue: every decision here was already made by the component it calls. Note what it does
*not* take — there is no target description, no profile, no map of the application. You give
it a URL and a goal in plain language; everything else the system either works out by
looking, or has learned from an earlier run.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

import click

from engine.config import load_env
from engine.discovery.engine import DiscoveryEngine
from engine.discovery.gemini_client import GeminiClient
from engine.discovery.llm_client import LLMError
from engine.discovery.loop_guard import LoopGuard
from engine.escalation.service import EscalationService
from engine.knowledge_base.service import KnowledgeBaseService
from engine.learning.outcome_learner import OutcomeLearner
from engine.policy.policy import DEFAULT_POLICY_PATH, Policy
from engine.recorder.recorder import Recorder
from engine.replay.engine import ReplayEngine
from engine.reporting.report_generator import ReportGenerator
from engine.schema.artifact import Artifact
from engine.storage.artifact_store import ArtifactStore
from engine.storage.escalation_store import EscalationStore
from engine.surface.playwright_surface import PlaywrightSurface


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _app_id(explicit: str | None, target: str) -> str:
    """Identify the application being driven.

    Defaults to the target's host, which is a serviceable identity for a single deployment.
    An operator running the same vendor product for several institutions passes `--app-id`
    explicitly so they share one Knowledge Base and one set of learned outcomes — that
    sharing is the whole multi-tenant story, and it hinges on this one string.
    """
    return explicit or (urlparse(target).netloc or "unknown").replace(":", "_")


def _parse_params(items: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"--param must look like key=value, got {item!r}")
        key, _, value = item.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


@click.group()
@click.option("--verbose", is_flag=True, help="Debug-level logging.")
@click.option("--headless", is_flag=True, help="Run the browser without a visible window.")
@click.option(
    "--policy",
    type=click.Path(exists=True),
    default=str(DEFAULT_POLICY_PATH),
    help="Safety policy file. The default is application-agnostic.",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, headless: bool, policy: str) -> None:
    """Discover capabilities against any web application, and replay known ones."""
    # Before any command reads a key or a target from the environment.
    load_env()
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["headless"] = headless
    ctx.obj["policy_path"] = policy


@cli.command()
@click.option("--goal", required=True, help="What to achieve, in plain language.")
@click.option("--target", required=True, help="Entry-point URL. The agent starts here knowing nothing else.")
@click.option("--capability-id", required=True, help="Id to save the compiled capability under.")
@click.option("--description", required=True, help="One line describing the capability.")
@click.option("--app-id", default=None, help="Groups runs that share a Knowledge Base [default: the target host].")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE",
              help="Values the goal needs (credentials, ids). Recorded as parameters, never as literals.")
@click.option("--secret", "secrets", multiple=True, metavar="KEY",
              help="Mark a --param as secret: never written to the artifact, logs or reports.")
@click.option("--requires", multiple=True, metavar="CAPABILITY_ID",
              help="Capability that must succeed first, e.g. a recorded login.")
@click.option("--max-steps", default=25, show_default=True)
@click.option("--timeout", default=180, show_default=True, help="Wall-clock budget, seconds.")
@click.option("--no-escalation", is_flag=True, help="Halt on risky actions instead of pausing for a human.")
@click.pass_context
def discover(ctx, goal, target, capability_id, description, app_id, params, secrets,
             requires, max_steps, timeout, no_escalation):
    """Run the LLM-driven discovery loop and record a capability from a successful run."""
    try:
        llm = GeminiClient()
    except LLMError as exc:
        raise click.ClickException(str(exc)) from exc

    app = _app_id(app_id, target)
    values = _parse_params(params)
    knowledge_base = KnowledgeBaseService()
    store = ArtifactStore()

    with PlaywrightSurface(
        base_url=target, headless=ctx.obj["headless"] or None
    ) as surface:
        escalation = (
            None
            if no_escalation
            else EscalationService(interactive=sys.stdin.isatty(), surface=surface)
        )
        # Any prerequisite capability runs first, so discovery begins from the state a
        # replay of this capability would also begin from.
        if requires:
            prerequisite_result = _establish(store, surface, list(requires), values)
            if prerequisite_result:
                raise click.ClickException(prerequisite_result)

        engine = DiscoveryEngine(
            surface=surface,
            llm_client=llm,
            policy=Policy(target, ctx.obj["policy_path"]),
            loop_guard=LoopGuard(max_steps=max_steps, timeout_seconds=timeout),
            app_id=app,
            knowledge_base=knowledge_base,
            escalation=escalation,
            report_generator=ReportGenerator(),
        )
        trace = engine.run(goal=goal, target_url=target, param_values=values)

    click.echo(f"\nDiscovery status : {trace.status}")
    click.echo(f"Steps            : {len(trace.steps)} ({trace.llm_calls} LLM calls)")
    click.echo(f"Report           : evidence/discovery_runs/{trace.run_id}/report.md")

    if trace.status != "completed":
        # A partial artifact is worse than none: it would look replayable and would not be.
        click.echo(f"Stop reason      : {trace.stop_reason}")
        click.echo("\nNo artifact saved — the run did not complete.")
        raise SystemExit(1)

    artifact = Recorder(knowledge_base).compile(
        trace, capability_id, description,
        secret_params=set(secrets), requires=list(requires),
    )
    path = store.save(artifact)
    click.echo(f"Artifact         : {path} (v{artifact.version}, {artifact.risk_class})")
    click.echo(f"Parameters       : {[p.name for p in artifact.input_params] or 'none'}")
    click.echo(
        f"Known outcomes   : {[o.code for o in artifact.known_outcomes] or 'none yet — '}"
        + ("" if artifact.known_outcomes else "the KB has not been taught any for this app")
    )


def _establish(store, surface, capability_ids, values) -> str | None:
    engine = ReplayEngine(surface, store=store)
    for capability_id in capability_ids:
        try:
            artifact = store.load(capability_id)
        except FileNotFoundError as exc:
            return str(exc)
        accepted = {p.name for p in artifact.input_params}
        result = engine.run(artifact, {k: v for k, v in values.items() if k in accepted})
        if result.status != "success":
            return f"prerequisite {capability_id!r} failed: {result.status} — {result.message}"
    return None


@cli.command()
@click.option("--capability", required=True, help="capability_id to invoke.")
@click.option("--target", envvar="ENGINE_TARGET", required=True,
              help="Origin of the deployment to run against, e.g. http://127.0.0.1:5050. "
                   "Artifacts store paths, not hosts, so the same capability replays against "
                   "any institution running the app. [env: ENGINE_TARGET]")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE",
              help="Input parameter; repeat for each one.")
@click.option("--artifact-file", type=click.Path(exists=True),
              help="Replay a specific artifact JSON instead of the stored capability.")
@click.option("--no-escalation", is_flag=True, help="Do not notify a human on failure.")
@click.pass_context
def replay(ctx, capability, target, params, artifact_file, no_escalation):
    """Execute a known capability deterministically — no LLM in this path."""
    store = ArtifactStore()
    if artifact_file:
        artifact = Artifact.model_validate(json.loads(Path(artifact_file).read_text()))
    else:
        try:
            artifact = store.load(capability)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc

    parsed = _parse_params(params)

    with PlaywrightSurface(
        base_url=target, headless=ctx.obj["headless"] or None
    ) as surface:
        escalation = (
            None
            if no_escalation
            else EscalationService(interactive=sys.stdin.isatty(), surface=surface)
        )
        engine = ReplayEngine(
            surface=surface,
            escalation=escalation,
            knowledge_base=KnowledgeBaseService(),
            report_generator=ReportGenerator(),
            store=store,
        )
        result = engine.run(artifact, parsed)

    click.echo(f"\nStatus  : {result.status}")
    if result.outcome_code:
        click.echo(f"Outcome : {result.outcome_code}")
    if result.outputs:
        click.echo(f"Outputs : {json.dumps(result.outputs)}")
    if result.message:
        click.echo(f"Detail  : {result.message}")
    click.echo(f"Report  : evidence/replay_runs/{result.run_id}/report.md")
    raise SystemExit(0 if result.status == "success" else 2)


@cli.command("learn-outcome")
@click.option("--app-id", required=True, help="Application this outcome belongs to.")
@click.option("--code", required=True, help="Short name, e.g. member_not_found.")
@click.option("--type", "outcome_type", required=True,
              type=click.Choice(["business_outcome", "recoverable", "hard_failure"]))
@click.option("--text-contains", default=None, help="Detector: substring of the page's visible text.")
@click.option("--url-contains", default=None, help="Detector: substring of the URL.")
@click.option("--message", default=None, help="What the caller should be told.")
@click.option("--recovery", default=None, help="Recovery hint, for recoverable outcomes.")
@click.option("--capability", default=None, help="Also teach this stored capability now (version bumped).")
@click.option("--from-escalation", default=None, help="Escalation id to take the run reference from.")
def learn_outcome(app_id, code, outcome_type, text_contains, url_contains, message,
                  recovery, capability, from_escalation):
    """Name a state the system met but did not recognise.

    This is how the Knowledge Base fills up. Nothing is declared in advance: a replay hits
    something it was never recorded to handle, reports it as a hard failure with candidate
    detectors, and a human decides what it actually means. Every capability recorded against
    this application afterwards inherits it.
    """
    if not (text_contains or url_contains):
        raise click.ClickException("give --text-contains or --url-contains")
    detector = {}
    if text_contains:
        detector["text_contains"] = text_contains
    if url_contains:
        detector["url_contains"] = url_contains

    learner = OutcomeLearner()
    try:
        outcome = learner.learn(
            app_id=app_id, code=code, outcome_type=outcome_type, detector=detector,
            message=message, recovery=recovery, capability_id=capability,
            learned_from=from_escalation,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Learned {outcome.code} ({outcome.type}) for app {app_id!r}.")
    click.echo(f"  detector: {detector}")
    if capability:
        click.echo(f"  {capability} updated to v{ArtifactStore().load(capability).version}")
    click.echo("Capabilities recorded for this app from now on will include it.")


@cli.command("list-artifacts")
def list_artifacts():
    """Show every capability currently in the Artifact Store."""
    store = ArtifactStore()
    ids = store.list()
    if not ids:
        click.echo("No artifacts recorded yet. Run `discover` first.")
        return
    click.echo(f"{'capability_id':<28} {'version':<9} {'status':<8} {'risk':<6} outcomes")
    for capability_id in ids:
        artifact = store.load(capability_id)
        click.echo(
            f"{artifact.capability_id:<28} {artifact.version:<9} {artifact.status:<8} "
            f"{artifact.risk_class:<6} {len(artifact.known_outcomes)}"
        )


@cli.command("list-escalations")
def list_escalations():
    """Show intervention requests, including any awaiting a human."""
    for request in EscalationStore().list_pending():
        click.echo(f"{request.request_id}  {request.source}  {request.reason}")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
