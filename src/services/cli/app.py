import sys
from pathlib import Path

from rich.console import Console
from textual.app import App

from orbitrows.services.cli.crash import report
from orbitrows.services.cli.screens import IntroScreen, SourceScreen


class OrbitRowsApp(App):
    TITLE = "OrbitRows"

    def __init__(self, root: Path = Path(".")):
        super().__init__()
        self.root = root
        self.source: Path | None = None
        self.rows: list[dict[str, str]] = []

    # ponytail: overrides Textual's private crash hook (textual 8.x); recheck on upgrade
    def _handle_exception(self, error: Exception) -> None:
        super()._handle_exception(error)  # exits the app; tests still get the real error
        self._exit_renderables[:] = [report(error)]  # replace the traceback Textual would print

    def on_mount(self) -> None:
        self.push_screen(SourceScreen(self.root))
        self.push_screen(IntroScreen())


def main() -> None:
    try:
        app = OrbitRowsApp(Path(sys.argv[1]) if sys.argv[1:] else Path("."))
        app.run()
    except Exception as e:  # errors outside the app loop (startup, terminal setup)
        Console(stderr=True).print(report(e))
        sys.exit(1)
    sys.exit(app.return_code or 0)
