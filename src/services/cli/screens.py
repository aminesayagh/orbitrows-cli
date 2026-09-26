import os
from pathlib import Path

from textual.app import ComposeResult
from textual.content import Content
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import Screen
from textual.widgets import Footer, Input, Static
from textual_autocomplete import DropdownItem, PathAutoComplete, TargetState

from orbitrows.services.csv.reader import load_csv

LOGO = """\
 ██████╗ ██████╗ ██████╗ ██╗████████╗██████╗  ██████╗ ██╗    ██╗███████╗
██╔═══██╗██╔══██╗██╔══██╗██║╚══██╔══╝██╔══██╗██╔═══██╗██║    ██║██╔════╝
██║   ██║██████╔╝██████╔╝██║   ██║   ██████╔╝██║   ██║██║ █╗ ██║███████╗
██║   ██║██╔══██╗██╔══██╗██║   ██║   ██╔══██╗██║   ██║██║███╗██║╚════██║
╚██████╔╝██║  ██║██████╔╝██║   ██║   ██║  ██║╚██████╔╝╚███╔███╔╝███████║
 ╚═════╝ ╚═╝  ╚═╝╚═════╝ ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝  ╚══╝╚══╝ ╚══════╝"""
GRADIENT = ["#5eead4", "#38bdf8", "#60a5fa", "#818cf8", "#a78bfa", "#c084fc"]

STEPS = """\
[b $accent]1[/]  Load your store export and the incoming supplier file
[b $accent]2[/]  Jev reads the columns and proposes how they map
[b $accent]3[/]  Rows are matched to products by SKU, name and variants
[b $accent]4[/]  You review every proposed change before anything is applied"""


class IntroScreen(Screen):
    BINDINGS = [("enter", "start", "Start"), ("q", "app.quit", "Quit")]
    DEFAULT_CSS = """
    IntroScreen { align: center middle; }
    IntroScreen > Vertical { width: auto; height: auto; opacity: 0; }
    #logo { width: auto; }
    #tagline { width: 100%; text-align: center; margin: 1 0 2 0; color: $text-muted; }
    #steps { width: auto; border: round $primary 50%; padding: 1 3; }
    #hint { width: 100%; text-align: center; margin-top: 2; text-style: bold; color: $accent; }
    """

    def compose(self) -> ComposeResult:
        logo = "\n".join(f"[{c}]{line}[/]" for c, line in zip(GRADIENT, LOGO.splitlines()))
        with Vertical():
            yield Center(Static(logo, id="logo"))
            yield Static("Keep your catalog in orbit: merge supplier CSVs into your store, safely.", id="tagline")
            yield Center(Static(STEPS, id="steps"))
            yield Static("Press Enter to begin", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Vertical).styles.animate("opacity", 1.0, duration=0.8)
        self.set_interval(0.8, self._blink)

    def _blink(self) -> None:
        hint = self.query_one("#hint")
        hint.styles.opacity = 0.35 if hint.styles.opacity == 1.0 else 1.0

    def action_start(self) -> None:
        self.app.pop_screen()


class CsvPathAutoComplete(PathAutoComplete):
    """Suggests folders and .csv files only, in a dropdown that opens above the input."""

    def get_candidates(self, target_state: TargetState) -> list[DropdownItem]:
        # Own listing instead of super(): the library stats every entry unguarded and
        # crashes on files it may not stat (e.g. /mnt/c/DumpStack.log.tmp under WSL).
        typed = target_state.text[: target_state.cursor_position]
        folder = self.path / Path(typed[: typed.rfind("/") + 1] or ".").expanduser()
        items = []
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if entry.name.startswith(".") or not (is_dir or entry.name.lower().endswith(".csv")):
                continue
            items.append((not is_dir, entry.name.lower(), entry.name + "/" * is_dir, is_dir))
        return [
            DropdownItem(name, prefix=self.folder_prefix if is_dir else self.file_prefix)
            for _, _, name, is_dir in sorted(items)
        ]

    # ponytail: overrides a private method of textual-autocomplete 4.x; recheck on upgrade
    def _align_to_target(self) -> None:
        x, _ = self.target.cursor_screen_offset
        _, height = self.option_list.outer_size
        box = self.target.parent.region  # the bordered prompt box
        self.absolute_offset = Offset(max(0, x - 2), max(0, box.y - height))


def message(text: str, bullet: str = "●", color: str = GRADIENT[3]) -> Static:
    return Static(f"[{color}]{bullet}[/] {text}", classes="message")


class SourceScreen(Screen):
    AUTO_FOCUS = "#path"
    BINDINGS = [("escape", "app.quit", "Quit")]
    DEFAULT_CSS = f"""
    SourceScreen {{ layout: vertical; }}
    #log {{ height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }}
    #banner {{ margin: 1 0; }}
    .message {{ margin-bottom: 1; }}
    #prompt {{ dock: bottom; height: auto; }}
    #box {{ height: 3; border: round {GRADIENT[4]}; padding: 0 1; }}
    #box:focus-within {{ border: round {GRADIENT[1]}; }}
    #caret {{ width: 2; color: {GRADIENT[1]}; text-style: bold; }}
    #path {{ border: none; background: transparent; padding: 0; height: 1; width: 1fr; }}
    #path:focus {{ border: none; background: transparent; }}
    #hints {{ padding: 0 2; color: $text-muted; }}
    """

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def compose(self) -> ComposeResult:
        log = VerticalScroll(id="log")
        log.can_focus = False
        with log:
            yield Static(
                f"[b {GRADIENT[1]}]✻[/] [b]OrbitRows[/]  [dim]·  source import  ·  {self.root.resolve()}[/]",
                id="banner",
            )
            yield message("Which CSV is your [b]store export[/]? Type its path below.")
        with Vertical(id="prompt"):
            with Horizontal(id="box"):
                yield Static(">", id="caret")
                path_input = Input(placeholder="data/store.csv", id="path")
                yield path_input
            yield Static("↑↓ browse · tab complete · enter load · esc quit", id="hints")
        yield CsvPathAutoComplete(
            path_input, path=self.root, show_dotfiles=False,
            folder_prefix=Content("▸ "), file_prefix=Content("  "),
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.submit(event.value)

    def say(self, *widgets: Static) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount_all(widgets)
        log.scroll_end(animate=False)

    def submit(self, value: str) -> None:
        if not value.strip():
            return
        path = self.root / Path(value.strip()).expanduser()  # absolute input overrides root
        echo = message(f"[dim]{value}[/]", ">", "$text-muted")
        self.query_one("#path", Input).clear()
        if path.suffix.lower() != ".csv" or not path.is_file():
            self.say(echo, message(f"Not a CSV file: {value}", "✗", "red"))
            return
        try:
            rows = load_csv(path)
        except ValueError as e:
            self.say(echo, message(f"Cannot read {e}", "✗", "red"))
            return
        self.app.source, self.app.rows = path, rows
        columns = "  ".join(
            f"[{GRADIENT[i % len(GRADIENT)]}]{name}[/]" for i, name in enumerate(rows[0])
        )
        # ponytail: next stage (column analysis) not built yet
        self.say(echo, message(
            f"Loaded [b]{path.name}[/]  [dim]{len(rows)} rows · {len(rows[0])} columns[/]\n  {columns}",
            "✓", GRADIENT[0],
        ))
