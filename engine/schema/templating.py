"""The `{{param}}` placeholder convention, and the only place that knows it.

This is a contract between three components that never call each other:

* **Discovery** types `{{member_id}}` at the keyboard, substituting the real value inside the
  Surface call so it never reaches the model (`render`).
* **The Recorder** does the inverse when compiling — a locator whose accessible name *is* the
  caller's value (`link:10007`) becomes `link:{{member_id}}`, so the step means "the member
  the caller named" rather than one specific member (`templatize`).
* **Replay** renders those placeholders back from its own params before resolving a locator
  (`render`, `render_locator`).

Before this module the substitution existed four times, written slightly differently each
time. That is a bad way to hold a convention: the delimiter is a format shared across a
recording boundary, so a change to it has to be atomic or artifacts recorded yesterday stop
resolving today. One owner makes that a single edit.
"""

from __future__ import annotations

import re

# A whole value that is nothing but a placeholder — used to recover which parameter a typed
# value came from, where a partial match would be meaningless.
WHOLE_PLACEHOLDER = re.compile(r"^\{\{(\w+)\}\}$")


def placeholder(name: str) -> str:
    """The placeholder text for a parameter. The one place the delimiter is written."""
    return f"{{{{{name}}}}}"


def param_name(text: str) -> str | None:
    """The parameter a value refers to, when the value is exactly one placeholder."""
    match = WHOLE_PLACEHOLDER.match((text or "").strip())
    return match.group(1) if match else None


def render(text: str, params: dict) -> str:
    """Replace every `{{name}}` with the caller's value for it.

    Values that carry no placeholder pass through untouched, which covers both stable
    locators (`textbox:Username`) and every artifact compiled before templating existed.
    """
    if not text or not params:
        return text
    for name, value in params.items():
        text = text.replace(placeholder(name), str(value))
    return text


def render_locator(locator: dict, params: dict) -> dict:
    """Render placeholders through a locator's primary rule and each of its fallbacks."""
    if not params:
        return locator

    def render_rule(rule: dict) -> dict:
        return {**rule, "value": render(rule.get("value", ""), params)}

    return {
        "primary": render_rule(locator["primary"]),
        "fallbacks": [render_rule(rule) for rule in locator.get("fallbacks") or []],
    }


def templatable(param_values: dict | None) -> list[tuple[str, str]]:
    """The caller's values as `(value, name)` pairs, longest value first.

    Ordering matters and is the reason this is built once rather than iterated ad hoc: a
    value which contains another — member_id "10002" contains the deposit "100" — must be
    matched before the shorter one, or a substitution already made gets corrupted. Values
    under three characters are excluded because they match far too much text to be evidence
    that the caller's parameter is what appears there.
    """
    return sorted(
        (
            (str(value), name)
            for name, value in (param_values or {}).items()
            if value and len(str(value)) >= 3
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )


def templatize(text: str, values: list[tuple[str, str]], *, as_template: bool = True) -> str:
    """Replace each embedded parameter value with its placeholder, or strip it out.

    The inverse of `render`. `as_template=False` drops the value instead of naming it, which
    is what naming an output wants — a member-scoped extract should not be immortalised as
    `member_detail_frank_osei_10007`.
    """
    if not text:
        return text
    for value, name in values:
        if value in text:
            text = text.replace(value, placeholder(name) if as_template else "")
    return text


def templatize_locator(locator: dict, values: list[tuple[str, str]]) -> dict:
    """Lift caller values out of a locator's primary rule and each of its fallbacks."""
    if not values:
        return locator

    def strip_rule(rule: dict) -> dict:
        return {**rule, "value": templatize(rule["value"], values)}

    return {
        "primary": strip_rule(locator["primary"]),
        "fallbacks": [strip_rule(rule) for rule in locator.get("fallbacks") or []],
    }
