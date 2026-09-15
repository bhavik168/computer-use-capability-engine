"""Playwright implementation of the Surface seam.

Observation is built on Playwright's ARIA snapshot — the browser-computed semantic layer
assistive technology reads, not the raw DOM. That choice is the whole reason this works
against legacy markup: the target app has table-based layout, no test ids and no useful
class names, but its controls still have real labels and button text, and the accessibility
computation turns those into stable `role + accessible name` pairs.

The raw DOM is used for exactly one thing: constructing a last-resort CSS locator when an
element has no usable accessible name (a bare balance cell, for instance).
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, sync_playwright

from engine.surface.base import Surface, SurfaceError
from engine.surface.elements import (
    INTERACTIVE_ROLES,
    READABLE_ROLES,
    VALUE_LIKE_RE,
    ObservedElement,
)

log = logging.getLogger(__name__)

# Everything that could plausibly be an interactive control or a readable value in this
# class of application. Deliberately generous — the ARIA role computed for each candidate
# is what actually decides whether it makes it into an observation.
CANDIDATE_SELECTOR = (
    "a, button, input:not([type=hidden]), select, textarea, td, th, h1, h2, h3, [role]"
)

# Wrapper cells in table-based layouts carry the accessible name of everything nested
# inside them, which is noise rather than signal. Anything this long is a layout container.
MAX_READABLE_NAME = 120

_HEADER_RE = re.compile(r'^([a-zA-Z]+)(?:\s+"((?:[^"\\]|\\.)*)")?')


def _parse_aria_header(snapshot: str) -> tuple[str, str] | None:
    """Pull `(role, accessible name)` out of a single element's ARIA snapshot."""
    if not snapshot:
        return None
    line = snapshot.splitlines()[0].strip()
    if line.startswith("- "):
        line = line[2:]
    if line.startswith("'"):
        end = line.find("':", 1)
        if end == -1:
            end = line.rfind("'")
        line = line[1:end].replace("''", "'")
    else:
        # Strip a trailing `: value` / `:` that introduces children.
        depth_quote = False
        for i, ch in enumerate(line):
            if ch == '"':
                depth_quote = not depth_quote
            elif ch == ":" and not depth_quote:
                line = line[:i]
                break
    match = _HEADER_RE.match(line.strip())
    if not match:
        return None
    role = match.group(1)
    name = match.group(2) or ""
    return role, name.replace('\\"', '"')


class PlaywrightSurface(Surface):
    def __init__(
        self,
        base_url: str,
        headless: bool | None = None,
        slow_mo: int = 0,
    ) -> None:
        # Accepts either an origin or a full entry-point URL; relative navigation resolves
        # against the origin either way.
        parsed = urlparse(base_url)
        self._base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else base_url.rstrip("/")
        # Non-headless by default: the visible window *is* the shared session a human takes
        # over during an escalation handoff, so there is no second console to build.
        if headless is None:
            headless = os.environ.get("ENGINE_HEADLESS", "").lower() in ("1", "true", "yes")
        self._headless = headless
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=headless, slow_mo=slow_mo
        )
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self._page = self._context.new_page()
        self._page.set_default_timeout(10_000)
        self._locators: dict[str, Locator] = {}
        self._observation: list[ObservedElement] = []
        self._last_strategy: str | None = None
        self._extra_seq = 0

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        for closer in (self._context.close, self._browser.close, self._playwright.stop):
            try:
                closer()
            except Exception:  # pragma: no cover - teardown is best-effort
                pass

    def __enter__(self) -> "PlaywrightSurface":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def page(self):
        """Escape hatch for dev utilities; engines go through the Surface methods."""
        return self._page

    # ---------------------------------------------------------------- observation

    def observe(self) -> list[ObservedElement]:
        elements: list[ObservedElement] = []
        self._locators = {}
        seq = 0
        seen: set[tuple[str, str, str]] = set()

        for frame in self._page.frames:
            try:
                candidates = frame.locator(CANDIDATE_SELECTOR)
                count = candidates.count()
            except PlaywrightError:
                continue  # frame detached mid-observation; nothing to see there

            for index in range(count):
                locator = candidates.nth(index)
                try:
                    parsed = _parse_aria_header(locator.aria_snapshot())
                except PlaywrightError:
                    continue
                if not parsed:
                    continue
                role, name = parsed

                interactive = role in INTERACTIVE_ROLES
                if not interactive and role not in READABLE_ROLES:
                    continue
                if not name and not interactive:
                    continue
                if not interactive and len(name) > MAX_READABLE_NAME:
                    continue  # a layout wrapper, not a value

                key = (frame.url, role, name)
                if key in seen:
                    continue  # legacy nesting repeats the same cell at several depths
                seen.add(key)

                try:
                    value = self._read_value(locator)
                    disabled = locator.is_disabled()
                except PlaywrightError:
                    value, disabled = None, False

                seq += 1
                element_id = f"e{seq}"
                self._locators[element_id] = locator
                elements.append(
                    ObservedElement(
                        element_id=element_id,
                        role=role,
                        name=name,
                        value=value,
                        disabled=disabled,
                        frame_url=frame.url,
                        interactive=interactive,
                    )
                )

        self._observation = elements
        self._extra_seq = 0
        return elements

    @staticmethod
    def _read_value(locator: Locator) -> str | None:
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        if tag in ("input", "textarea", "select"):
            return locator.input_value() or None
        return None

    def current_url(self) -> str:
        return self._page.url

    def visible_text(self) -> str:
        chunks: list[str] = []
        for frame in self._page.frames:
            try:
                chunks.append(frame.locator("body").inner_text())
            except PlaywrightError:
                continue
        return "\n".join(chunk for chunk in chunks if chunk)

    def screenshot(self) -> bytes:
        return self._page.screenshot()

    # ---------------------------------------------------------------- actions

    def navigate(self, url: str) -> None:
        absolute = url if urlparse(url).scheme else urljoin(self.base_url + "/", url.lstrip("/"))
        self._page.goto(absolute, wait_until="domcontentloaded")

    def click(self, element_id: str) -> None:
        locator = self._require(element_id)
        locator.click()
        self._page.wait_for_load_state("domcontentloaded")

    def type_text(self, element_id: str, text: str) -> None:
        self._require(element_id).fill(text)

    def read_text(self, element_id: str) -> str:
        locator = self._require(element_id)
        value = self._read_value(locator)
        if value is not None:
            return value
        return (locator.inner_text() or "").strip()

    def _require(self, element_id: str) -> Locator:
        locator = self._locators.get(element_id)
        if locator is None:
            raise SurfaceError(
                f"unknown element_id {element_id!r}; it was not in the last observation"
            )
        return locator

    # ---------------------------------------------------------------- locator construction

    CSS_PATH_JS = """el => {
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'body') {
            let index = 1;
            let sibling = node.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === node.tagName) index++;
                sibling = sibling.previousElementSibling;
            }
            parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
            node = node.parentElement;
        }
        return 'body > ' + parts.join(' > ');
    }"""

    def build_locator(self, element_id: str) -> dict:
        locator = self._require(element_id)
        element = next(
            (el for el in self._observation if el.element_id == element_id), None
        )
        role_name = None
        if element is not None and element.name:
            role_name = {"strategy": "role+name", "value": f"{element.role}:{element.name}"}

        css = None
        try:
            css = {"strategy": "css", "value": locator.evaluate(self.CSS_PATH_JS)}
        except PlaywrightError:
            pass

        # A name shared with another element on screen cannot identify anything on its own.
        ambiguous = (
            element is not None
            and sum(
                1
                for other in self._observation
                if other.role == element.role and other.name == element.name
            )
            > 1
        )

        # An accessible name that is the element's own value is not identity — it is data.
        name_is_value = bool(
            element is not None
            and not element.interactive
            and element.name
            and VALUE_LIKE_RE.match(element.name.strip())
        )

        if role_name and not name_is_value and not ambiguous:
            return {"primary": role_name, "fallbacks": [css] if css else []}
        if css:
            return {"primary": css, "fallbacks": [role_name] if role_name else []}
        if role_name:
            return {"primary": role_name, "fallbacks": []}
        raise SurfaceError(f"cannot build a locator for {element_id!r}")

    # ---------------------------------------------------------------- locator resolution

    def last_resolution_strategy(self) -> str | None:
        return self._last_strategy

    def resolve_locator(self, locator: dict) -> str | None:
        """Resolve an artifact locator against the page as it is right now."""
        self._last_strategy = None
        rules: list[tuple[str, dict]] = []
        primary = locator.get("primary")
        if primary:
            rules.append(("primary", primary))
        for i, fallback in enumerate(locator.get("fallbacks") or []):
            rules.append((f"fallback[{i}]", fallback))

        self.observe()
        for label, rule in rules:
            element_id = self._try_rule(rule)
            if element_id:
                self._last_strategy = label
                if label != "primary":
                    # Drift signal: the accessible name changed, or the element moved.
                    log.warning(
                        "locator resolved via %s (%s=%s), not primary %s",
                        label,
                        rule.get("strategy"),
                        rule.get("value"),
                        (primary or {}).get("value"),
                    )
                return element_id
        return None

    def _try_rule(self, rule: dict) -> str | None:
        strategy = rule.get("strategy")
        value = rule.get("value") or ""
        if strategy == "role+name":
            role, _, name = value.partition(":")
            matches = [
                el
                for el in self._observation
                if el.role == role.strip() and el.name.strip().lower() == name.strip().lower()
            ]
            return matches[0].element_id if len(matches) == 1 else None
        if strategy == "css":
            return self._resolve_dom(lambda frame: frame.locator(value))
        if strategy == "text":
            return self._resolve_dom(lambda frame: frame.get_by_text(value, exact=False))
        log.warning("unknown locator strategy %r", strategy)
        return None

    def _resolve_dom(self, build) -> str | None:
        """Resolve a raw-DOM style locator across every frame; must be unambiguous.

        DOM-derived locators are the last resort in the chain, and they frequently point at
        something the accessibility tree never surfaced (a bare table cell holding a
        balance). Such an element gets its own `x<n>` id in the current id space rather
        than being forced to correspond to an observed element.
        """
        hits: list[Locator] = []
        for frame in self._page.frames:
            try:
                candidate = build(frame)
                count = candidate.count()
            except PlaywrightError:
                continue
            if count == 1:
                hits.append(candidate)
            elif count > 1:
                return None  # ambiguous: refuse rather than guess which one was meant
        if len(hits) != 1:
            return None

        self._extra_seq += 1
        element_id = f"x{self._extra_seq}"
        self._locators[element_id] = hits[0]
        return element_id
