"""Dev utility: prove the Surface can see and act on an arbitrary page.

    python -m engine.surface._manual_check http://127.0.0.1:5050/login

Prints what the accessibility tree exposes, and the persistent locator the engine would
record for each element. Nothing here knows anything about the page it is pointed at — that
is the point of the check.
"""

from __future__ import annotations

import logging
import sys

from engine.surface.playwright_surface import PlaywrightSurface


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5050/login"

    with PlaywrightSurface(base_url=url) as surface:
        surface.navigate(url)
        print(f"\n=== {surface.current_url()}")
        for element in surface.observe():
            print("   ", element.describe())
            if element.interactive:
                locator = surface.build_locator(element.element_id)
                print(f"        locator: {locator['primary']}")


if __name__ == "__main__":
    main()
