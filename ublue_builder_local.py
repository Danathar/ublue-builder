#!/usr/bin/env python3
from __future__ import annotations

from ublue_builder import App, CommandError


class LocalUtilityApp(App):
    def banner(self) -> None:
        print(
            self.gum.style(
                "Universal Blue Builder  Local Utilities",
                "",
                "Build locally or set up nightly rebuilds.",
                "Advanced tool for local and unattended workflows.",
                align="center",
                width=68,
                margin="1 2",
                padding="1 2",
                foreground=11,
                border_foreground=11,
                border="double",
            )
        )

    def main_menu(self) -> None:
        action = self.gum.choose(
            [
                "Build & Install Locally",
                "Set Up Nightly Local Build",
                "Quit",
            ],
            height=6,
        )
        selected = action[0] if action else "Quit"
        if selected == "Build & Install Locally":
            self.local_build_image()
            return
        if selected == "Set Up Nightly Local Build":
            self.setup_nightly_build()
            return
        raise SystemExit(0)

    def run_main(self) -> None:
        self.clear()
        self.banner()
        self.preflight()
        self.main_menu()


def main() -> None:
    app = LocalUtilityApp()
    try:
        app.run_main()
    except CommandError as exc:
        app.gum.error(str(exc))
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


if __name__ == "__main__":
    main()
