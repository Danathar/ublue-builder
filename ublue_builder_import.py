#!/usr/bin/env python3
from __future__ import annotations

from ublue_builder import App, CommandError, ScreenBack


class LegacyImportApp(App):
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
        self.clear()
        self.banner()
        self.preflight()
        self.import_legacy_repo()


def main() -> None:
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
