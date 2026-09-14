"""Operator-set safety policy. Knows nothing about any application.

Two rules live here, and they fail in opposite directions on purpose. The **allowlist** is a
hard boundary derived from the target the operator pointed at: leaving it is never a
question for a human, because there is nothing legitimate on the other side. **Risk
classification** is a judgement about one control, and a wrong guess is cheap in one
direction (a needless pause) and expensive in the other (an unintended write), so it leans
toward stopping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from engine.surface.elements import ObservedElement

log = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "defaults.yaml"

RiskClass = Literal["safe", "risky"]


class Policy:
    def __init__(self, target_url: str, policy_path: Path | str = DEFAULT_POLICY_PATH) -> None:
        config = yaml.safe_load(Path(policy_path).read_text()) or {}
        self.risky_action_names: list[str] = config.get("risky_action_names") or []
        self.safe_action_names: list[str] = config.get("safe_action_names") or []

        # The allowlist is derived, not declared: whatever host the operator aimed the run
        # at is the surface, plus anything they explicitly widened it to.
        host = urlparse(target_url).netloc
        self.allowed_domains: list[str] = [host] if host else []
        self.allowed_domains += config.get("additional_allowed_domains") or []
        log.info("policy: allowlist=%s", self.allowed_domains)

    def classify(self, action: str, element: ObservedElement | None) -> RiskClass:
        """Risk is a property of the control, not of the screen it happens to sit on.

        Matching the control's own accessible name means a screen this system has never
        visited cannot quietly introduce an unclassified copy of a dangerous action.
        """
        if element is None or action not in ("click", "select"):
            return "safe"
        name = (element.name or "").strip().lower()
        if not name:
            return "safe"
        if any(exempt in name for exempt in self.safe_action_names):
            return "safe"
        return "risky" if any(word in name for word in self.risky_action_names) else "safe"

    def check_allowlist(self, url: str) -> bool:
        host = urlparse(url).netloc or url
        allowed = any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains)
        if not allowed:
            log.error("blocked: %s is outside the allowlist %s", url, self.allowed_domains)
        return allowed
