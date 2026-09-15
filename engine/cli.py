"""Capability API / CLI — the single entry point an agent (or an operator) drives.

Pure glue: every decision here was already made by the component it calls. Note what it does
*not* take — there is no target description, no profile, no map of the application. You give
it a URL and a goal in plain language; everything else the system either works out by
looking, or has learned from an earlier run.
"""

from __future__ import annotations

import functools
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
from engine.intent import (
    IntentParseError,
    execute_plan,
    format_plan_for_confirmation,
    parse_intent,
    reconcile_with_store,
)
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


log = logging.getLogger(__name__)


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
    result = _run_discovery(
        goal=goal,
        target=target,
        capability_id=capability_id,
        description=description,
        params=_parse_params(params),
        secret_params=set(secrets),
        requires=list(requires),
        app_id=app_id,
        max_steps=max_steps,
        timeout=timeout,
        headless=ctx.obj["headless"],
        policy_path=ctx.obj["policy_path"],
        escalation_enabled=not no_escalation,
    )
    _echo_discovery(result)
    if result["status"] != "success":
        raise SystemExit(1)


def _run_discovery(
    *,
    goal: str,
    target: str,
    capability_id: str,
    description: str,
    params: dict[str, str],
    secret_params: set[str],
    requires: list[str],
    app_id: str | None = None,
    max_steps: int = 25,
    timeout: int = 180,
    headless: bool = False,
    policy_path: str = str(DEFAULT_POLICY_PATH),
    escalation_enabled: bool = True,
) -> dict:
    """One discovery run, in process.

    Split out of the `discover` command so the intent planner can invoke exactly the
    path an operator would have typed, rather than a parallel implementation of it that
    would drift. The command keeps the flag parsing and the exit code; everything that
    actually runs lives here, and the result is returned as data instead of printed, so
    a caller running several capabilities in a plan can decide what to do next.
    """
    try:
        llm = GeminiClient()
    except LLMError as exc:
        raise click.ClickException(str(exc)) from exc

    app = _app_id(app_id, target)
    knowledge_base = KnowledgeBaseService()
    store = ArtifactStore()

    with PlaywrightSurface(base_url=target, headless=headless or None) as surface:
        escalation = (
            EscalationService(interactive=sys.stdin.isatty(), surface=surface)
            if escalation_enabled
            else None
        )
        # Any prerequisite capability runs first, so discovery begins from the state a
        # replay of this capability would also begin from.
        if requires:
            prerequisite_result = _establish(store, surface, list(requires), params)
            if prerequisite_result:
                raise click.ClickException(prerequisite_result)

        engine = DiscoveryEngine(
            surface=surface,
            llm_client=llm,
            policy=Policy(target, policy_path),
            loop_guard=LoopGuard(max_steps=max_steps, timeout_seconds=timeout),
            app_id=app,
            knowledge_base=knowledge_base,
            escalation=escalation,
            report_generator=ReportGenerator(),
        )
        trace = engine.run(goal=goal, target_url=target, param_values=params)

    result = {
        "kind": "discovery",
        "capability_id": capability_id,
        "trace_status": trace.status,
        "steps": len(trace.steps),
        "llm_calls": trace.llm_calls,
        "report": f"evidence/discovery_runs/{trace.run_id}/report.md",
        "run_id": trace.run_id,
    }

    if trace.status != "completed":
        # A partial artifact is worse than none: it would look replayable and would not be.
        result.update(status="hard_failure", message=trace.stop_reason, artifact=None)
        return result

    artifact = Recorder(knowledge_base).compile(
        trace, capability_id, description,
        secret_params=set(secret_params), requires=list(requires),
        param_values=params,
    )
    path = store.save(artifact)
    result.update(
        status="success",
        artifact=str(path),
        version=artifact.version,
        risk_class=artifact.risk_class,
        input_params=[p.name for p in artifact.input_params],
        known_outcomes=[o.code for o in artifact.known_outcomes],
        outputs={o.name: o.type for o in artifact.outputs},
    )
    return result


def _echo_discovery(result: dict) -> None:
    click.echo(f"\nDiscovery status : {result['trace_status']}")
    click.echo(f"Steps            : {result['steps']} ({result['llm_calls']} LLM calls)")
    click.echo(f"Report           : {result['report']}")

    if result["status"] != "success":
        click.echo(f"Stop reason      : {result['message']}")
        click.echo("\nNo artifact saved — the run did not complete.")
        return

    click.echo(f"Artifact         : {result['artifact']} (v{result['version']}, {result['risk_class']})")
    click.echo(f"Parameters       : {result['input_params'] or 'none'}")
    click.echo(
        f"Known outcomes   : {result['known_outcomes'] or 'none yet — '}"
        + ("" if result["known_outcomes"] else "the KB has not been taught any for this app")
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
    result = _run_replay(
        capability_id=capability,
        target=target,
        params=_parse_params(params),
        artifact_file=artifact_file,
        headless=ctx.obj["headless"],
        escalation_enabled=not no_escalation,
    )
    _echo_replay(result)
    raise SystemExit(0 if result["status"] == "success" else 2)


def _run_replay(
    *,
    capability_id: str,
    target: str,
    params: dict[str, str],
    artifact_file: str | None = None,
    headless: bool = False,
    escalation_enabled: bool = True,
    drop_unknown_params: bool = False,
) -> dict:
    """One replay run, in process. Counterpart to `_run_discovery`.

    `drop_unknown_params` exists for the intent planner and is off for the command. A
    human who passes a parameter a capability does not declare has made a mistake worth
    hearing about; a plan, by contrast, carries every value the operator mentioned in
    one sentence and hands the same bag to each call in turn, so the lookup receiving a
    `password` it has no use for is the normal case rather than an error. What each
    capability accepts is still read from the artifact — including the parameters its
    prerequisites declare, which is how a login inside a `requires` chain gets its
    credentials without the calling capability declaring them.
    """
    store = ArtifactStore()
    if artifact_file:
        artifact = Artifact.model_validate(json.loads(Path(artifact_file).read_text()))
    else:
        try:
            artifact = store.load(capability_id)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc

    if drop_unknown_params:
        accepted = _accepted_params(store, artifact)
        dropped = sorted(set(params) - accepted)
        if dropped:
            # Names only: a dropped value may well be the password.
            log.info("%s does not take %s; not passing it", artifact.capability_id, dropped)
        params = {k: v for k, v in params.items() if k in accepted}

    with PlaywrightSurface(base_url=target, headless=headless or None) as surface:
        escalation = (
            EscalationService(interactive=sys.stdin.isatty(), surface=surface)
            if escalation_enabled
            else None
        )
        engine = ReplayEngine(
            surface=surface,
            escalation=escalation,
            knowledge_base=KnowledgeBaseService(),
            report_generator=ReportGenerator(),
            store=store,
        )
        result = engine.run(artifact, params)

    return {
        "kind": "replay",
        "capability_id": artifact.capability_id,
        "status": result.status,
        "outcome_code": result.outcome_code,
        "outputs": result.outputs,
        "message": result.message,
        "run_id": result.run_id,
        "report": f"evidence/replay_runs/{result.run_id}/report.md",
    }


def _accepted_params(store: ArtifactStore, artifact: Artifact) -> set[str]:
    """Parameter names this artifact or any capability it requires declares."""
    names = {p.name for p in artifact.input_params}
    for required in artifact.requires:
        prerequisite = store.get_latest(required)
        if prerequisite is not None:
            names |= _accepted_params(store, prerequisite)
    return names


def _echo_replay(result: dict) -> None:
    click.echo(f"\nStatus  : {result['status']}")
    if result["outcome_code"]:
        click.echo(f"Outcome : {result['outcome_code']}")
    if result["outputs"]:
        click.echo(f"Outputs : {json.dumps(result['outputs'])}")
    if result["message"]:
        click.echo(f"Detail  : {result['message']}")
    click.echo(f"Report  : {result['report']}")


@cli.command("run")
@click.option("--prompt", required=True,
              help="What you want done, in plain language. The plan is shown before anything runs.")
@click.option("--target", envvar="ENGINE_TARGET", required=True,
              help="Entry-point URL of the deployment to run against. [env: ENGINE_TARGET]")
@click.option("--app-id", default=None, help="Groups runs that share a Knowledge Base [default: the target host].")
@click.option("--yes", is_flag=True, help="Execute without asking. For scripted and CI use.")
@click.option("--dry-run", is_flag=True, help="Print the plan and stop. Never opens a browser.")
@click.option("--max-steps", default=25, show_default=True, help="Per discovery call.")
@click.option("--timeout", default=180, show_default=True, help="Wall-clock budget per discovery call, seconds.")
@click.option("--no-escalation", is_flag=True, help="Halt on risky actions instead of pausing for a human.")
@click.pass_context
def run(ctx, prompt, target, app_id, yes, dry_run, max_steps, timeout, no_escalation):
    """Do what a plain-language goal asks, planning the capabilities it needs.

    The layer the other commands were always underneath. One model call turns the goal
    into an ordered plan — which capabilities, in what order, replaying what is already
    recorded and discovering only what is not — and the plan is printed and confirmed
    before a browser opens. Nothing here decides how to drive the application; that is
    still the Discovery Engine's job, and a capability it already recorded is still
    replayed with no model in the loop.
    """
    try:
        llm = GeminiClient()
    except LLMError as exc:
        raise click.ClickException(str(exc)) from exc

    store = ArtifactStore()
    catalog = store.list_capabilities()

    try:
        calls = parse_intent(prompt, catalog, llm)
    except (IntentParseError, LLMError) as exc:
        raise click.ClickException(f"could not turn that into a plan: {exc}") from exc

    # What is actually on disk decides replay vs discover, so say so before asking.
    reconcile_with_store(calls, store)

    click.echo(f"\nPlan ({len(calls)} call{'s' if len(calls) != 1 else ''}) against {target}:")
    click.echo(format_plan_for_confirmation(calls))

    if dry_run:
        return
    if not yes and not click.confirm("\nProceed?", default=False):
        click.echo("Nothing ran.")
        return

    discover_fn = functools.partial(
        _run_discovery,
        app_id=app_id,
        max_steps=max_steps,
        timeout=timeout,
        headless=ctx.obj["headless"],
        policy_path=ctx.obj["policy_path"],
        escalation_enabled=not no_escalation,
    )
    replay_fn = functools.partial(
        _run_replay,
        headless=ctx.obj["headless"],
        escalation_enabled=not no_escalation,
        drop_unknown_params=True,
    )

    results = execute_plan(calls, target, store, discover_fn, replay_fn)

    click.echo("\n" + "-" * 60)
    for call, result in zip(calls, results):
        click.echo(f"\n{call.capability_id}: {result['status']}")
        if result.get("outputs"):
            click.echo(f"  outputs : {json.dumps(result['outputs'])}")
        if result.get("outcome_code"):
            click.echo(f"  outcome : {result['outcome_code']}")
        if result.get("message"):
            click.echo(f"  detail  : {result['message']}")
        if result.get("report"):
            click.echo(f"  report  : {result['report']}")

    raise SystemExit(0 if all(r["status"] == "success" for r in results) else 2)


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
