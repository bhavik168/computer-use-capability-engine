"""The single seam through which every engine drives the target application.

Nothing above this layer — the discovery loop, the artifact schema, the replay executor —
knows whether the surface underneath is a browser today or a desktop driver tomorrow. That
is the entire point of the abstraction (see ARCHITECTURE.md §5). A desktop driver would be
a second implementation of this ABC and would touch nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from engine.surface.elements import ObservedElement


class SurfaceError(RuntimeError):
    """Raised when the surface cannot carry out an instruction against the target."""


class Surface(ABC):
    """Perceive and act. Nothing more.

    There is deliberately no `login()` here. Authentication is not a property of a surface —
    it is something one particular application happens to require, reached by finding and
    operating controls like any other goal. Giving this interface an auth concept would bake
    in an assumption that every target has a login form shaped the way the first one did, and
    would make credentials the engine's business rather than the caller's.
    """

    @abstractmethod
    def observe(self) -> list[ObservedElement]:
        """Enumerate what is on screen right now, with fresh synthetic element ids."""

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def visible_text(self) -> str:
        """All visible text on the page, including inside child frames.

        Used by `known_outcomes` detectors of kind `text_contains`.
        """

    @abstractmethod
    def screenshot(self) -> bytes: ...

    @abstractmethod
    def navigate(self, url: str) -> None: ...

    @abstractmethod
    def click(self, element_id: str) -> None: ...

    @abstractmethod
    def type_text(self, element_id: str, text: str) -> None: ...

    @abstractmethod
    def read_text(self, element_id: str) -> str:
        """Current text content of an observed element — how `extract` steps read values."""

    @abstractmethod
    def build_locator(self, element_id: str) -> dict:
        """Construct a persistent, replay-usable locator for an observed element.

        This is where a per-observation synthetic id becomes something an artifact can
        carry. `role + name` leads wherever the accessible name is a *label*; where the
        name is the element's own changing value (a balance cell), a DOM-derived path
        leads instead, with role+name kept as the fallback — a locator keyed on the value
        it is meant to read would only ever resolve for the member it was recorded against.
        """

    @abstractmethod
    def resolve_locator(self, locator: dict) -> str | None:
        """Resolve a persistent artifact locator against the *current* page.

        Tries `primary`, then each entry of `fallbacks` in order, and returns the
        `element_id` of the first strategy that matches exactly one element in a fresh
        observation — or None if nothing resolves. Replay uses this rather than the
        synthetic per-observation ids, because those mean nothing across runs.
        """

    @abstractmethod
    def last_resolution_strategy(self) -> str | None:
        """Which strategy satisfied the most recent `resolve_locator` call.

        `"primary"`, `"fallback[n]"`, or None. A run that leans on fallbacks is the drift
        signal the Knowledge Base consumes later.
        """

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Origin of the deployment being driven, e.g. `https://corebank.example`.

        Part of the interface rather than an implementation detail because artifacts store
        *paths*, not hosts — that is what lets one capability replay against any institution
        running the same application — so anything resolving a recorded step's relative URL
        needs the deployment it is being resolved against. A desktop surface answers with
        whatever plays the role of an origin for it (an application identifier), and a
        surface with no such concept returns "".
        """

    # ---------------------------------------------------------------- best-effort reads
    #
    # Concrete, and deliberately not abstract: every caller wants "tell me the URL, and if
    # the surface is mid-navigation or already torn down, say nothing rather than raising."
    # Both engines had private copies of exactly this try/except, which meant the rule that
    # observability must never be the thing that breaks a run was asserted in two places and
    # owned by neither.

    def safe_url(self) -> str | None:
        """`current_url()`, or None if the surface cannot answer right now."""
        try:
            return self.current_url()
        except Exception:  # noqa: BLE001 — a failed read must never fail the run
            return None

    def safe_screenshot(self) -> bytes | None:
        """`screenshot()`, or None if one cannot be taken right now."""
        try:
            return self.screenshot()
        except Exception:  # noqa: BLE001 — evidence is best-effort by definition
            return None
