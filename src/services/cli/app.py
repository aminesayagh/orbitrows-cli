import sys
from functools import cached_property
from pathlib import Path

from rich.console import Console
from textual.app import App

from orbitrows.services.cli.crash import report
from orbitrows.services.cli.screens import IntroScreen, PromptScreen
from orbitrows.services.jev.client import connect


class OrbitRowsApp(App):
    TITLE = "OrbitRows"

    def __init__(self, root: Path = Path(".")):
        super().__init__()
        self.root = root
        # ponytail: in memory, a session starts fresh each run; nothing is persisted until storage is designed
        self.files: dict[str, list[dict]] = {}  # "store"/"incoming" -> [{"name", "columns"}], what Jev sees
        self.store_id: str | None = None  # the store's id in the registry (stores.db)
        self.context: dict[str, dict] = {}  # file name -> its column profile (Jev + the user's conflict answers)

    @cached_property
    def jev(self):
        return connect()

    # ponytail: overrides Textual's private crash hook (textual 8.x); recheck on upgrade
    def _handle_exception(self, error: Exception) -> None:
        super()._handle_exception(error)  # exits the app; tests still get the real error
        self._exit_renderables[:] = [report(error)]  # replace the traceback Textual would print

    def on_mount(self) -> None:
        self.push_screen(PromptScreen(self.root))
        self.push_screen(IntroScreen())


def main() -> None:
    try:
        app = OrbitRowsApp(Path(sys.argv[1]) if sys.argv[1:] else Path("."))
        app.run()
    except Exception as e:  # errors outside the app loop (startup, terminal setup)
        Console(stderr=True).print(report(e))
        sys.exit(1)
    sys.exit(app.return_code or 0)
