#!/usr/bin/env python3
from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required.")

from ublue_builder import App, CommandError, ScreenBack


class LegacyImportApp(App):
    # This subclass exists to expose the advanced "adopt an older repo" flow as
    # a separate entrypoint. Keeping it separate from the beginner app reduces
    # the chance that a new user stumbles into a heuristic import by mistake.
    def banner(self) -> None:
        print(
            self.gum.style(
                "uBlue Builder  Legacy Import",
                "",
                "Adopt an existing image repo into this tool.",
                "Advanced beta tool for older or manually created repos.",
                align="center",
                width=self.gum.content_width(reserve=8),
                margin="1 2",
                padding="1 2",
                foreground=11,
                border_foreground=11,
                border="double",
            )
        )

    def run_main(self) -> None:
        # The legacy tool skips the normal main menu and goes straight into the
        # import workflow after the same startup/preflight steps.
        self.clear()
        self.banner()
        self.preflight()
        self.import_legacy_repo()


def main() -> None:
    # Match the main app's top-level exception handling so Ctrl+C, back/cancel,
    # and command failures all behave consistently across entrypoints.
    app = LegacyImportApp()
    try:
        app.run_main()
    except ScreenBack:
        print()
        raise SystemExit(0)
    except CommandError as exc:
        app.gum.error(str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    main()
