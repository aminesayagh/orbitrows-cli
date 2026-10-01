import asyncio
import os
from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.content import Content
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static
from textual_autocomplete import DropdownItem, PathAutoComplete, TargetState
from sqlalchemy.exc import OperationalError
from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.context import classify_columns
from orbitrows.services.csv.reader import load_csv
from orbitrows.services.db import workspace
from orbitrows.services.jev.intent import classify

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


def word_at_cursor(state: TargetState) -> tuple[int, TargetState]:
    """The whitespace-separated word under the cursor, so a path can be completed inside a prompt."""
    start = state.text.rfind(" ", 0, state.cursor_position) + 1
    return start, TargetState(state.text[start : state.cursor_position], state.cursor_position - start)


class CsvPathAutoComplete(PathAutoComplete):
    """Completes the word under the cursor to folders and .csv files, in a dropdown above the input."""

    def get_candidates(self, target_state: TargetState) -> list[DropdownItem]:
        # Own listing instead of super(): the library stats every entry unguarded and
        # crashes on files it may not stat (e.g. /mnt/c/DumpStack.log.tmp under WSL).
        typed = word_at_cursor(target_state)[1].text
        if not typed:  # between words of a prompt: stay out of the way
            return []
        folder = self.path / Path(typed[: typed.rfind("/") + 1] or ".").expanduser()
        segment = typed[typed.rfind("/") + 1 :].lower()
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
            if not entry.name.lower().startswith(segment):  # prefix only: prompt words must not fuzzy-match files
                continue
            items.append((not is_dir, entry.name.lower(), entry.name + "/" * is_dir, is_dir))
        return [
            DropdownItem(name, prefix=self.folder_prefix if is_dir else self.file_prefix)
            for _, _, name, is_dir in sorted(items)
        ]

    def get_search_string(self, target_state: TargetState) -> str:
        return super().get_search_string(word_at_cursor(target_state)[1])

    def apply_completion(self, value: str, state: TargetState) -> None:
        start, word = word_at_cursor(state)
        before = state.text[:start] + word.text[: word.text.rfind("/") + 1] + value
        with self.prevent(Input.Changed):
            self.target.value = before + state.text[state.cursor_position :]
            self.target.cursor_position = len(before)

    def post_completion(self) -> None:
        if not self.target.value[: self.target.cursor_position].endswith("/"):
            self.action_hide()

    # ponytail: overrides a private method of textual-autocomplete 4.x; recheck on upgrade
    def _align_to_target(self) -> None:
        x, _ = self.target.cursor_screen_offset
        _, height = self.option_list.outer_size
        box = self.target.parent.region  # the bordered prompt box
        self.absolute_offset = Offset(max(0, x - 2), max(0, box.y - height))


def message(text: str, bullet: str = "●", color: str = GRADIENT[3]) -> Static:
    return Static(f"[{color}]{bullet}[/] {text}", classes="message")


class Refusal(Exception):
    """A request we can't do; its text is shown to the user as is."""


def split_input(value: str, root: Path) -> tuple[str, Path | None]:
    """(prompt, csv path). A word ending in .csv is the file, the other words are the prompt."""
    # ponytail: paths with spaces aren't supported; quote-aware parsing if users need them
    words = value.split()
    files = [w for w in words if w.lower().endswith(".csv")]
    if len(files) > 1:
        raise Refusal("One file at a time, please.")
    prompt = " ".join(w for w in words if w not in files)
    return prompt, (root / Path(files[0]).expanduser() if files else None)  # absolute path overrides root


def need(variable: str) -> None:
    if not os.environ.get(variable):
        raise Refusal(f"{variable} is not set. Add it to .env and start with: uv run --env-file .env orbitrows")


COMMANDS = """\
[b]/preview[/]          show the latest version of the store
[b]/undo store[/]       undo the last change to the store
[b]/undo incoming[/]    undo the last change to the incoming file
[b]/merge[/]            merge the incoming file into the store
[b]/reset[/]            go back to the files as they were uploaded"""

PREVIEW_ROWS, PREVIEW_COLUMNS = 20, 6


def context_summary(context: dict, name) -> str:
    """One line per column that carries a context. Flags (uncertain, unresolved) wait for the merge."""
    lines = []
    for key, c in context.items():
        if c.role in ("dedicated_context", "both") and c.category:
            described = [name(k) for k, other in context.items() if other.client == key]
            unit = f" {c.header_context}" if c.header_context else ""
            lines.append(f"{name(key)} → {c.category}{unit}" + (f"  [dim]for {', '.join(described)}[/]" if described else ""))
        elif c.role == "header_context" and c.header_context:
            lines.append(f"{name(key)} → {c.category} {c.header_context}")
    return "Column context\n  " + "\n  ".join(lines) if lines else "No unit, currency or language columns found."


class PromptScreen(Screen):
    AUTO_FOCUS = "#entry"
    BINDINGS = [("escape", "app.quit", "Quit")]
    DEFAULT_CSS = f"""
    PromptScreen {{ layout: vertical; }}
    #log {{ height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }}
    #banner {{ margin: 1 0; }}
    .message {{ margin-bottom: 1; }}
    .preview {{ height: auto; max-height: 22; margin-bottom: 1; }}
    #prompt {{ dock: bottom; height: auto; }}
    #box {{ height: 3; border: round {GRADIENT[4]}; padding: 0 1; }}
    #box:focus-within {{ border: round {GRADIENT[1]}; }}
    #caret {{ width: 2; color: {GRADIENT[1]}; text-style: bold; }}
    #entry {{ border: none; background: transparent; padding: 0; height: 1; width: 1fr; }}
    #entry:focus {{ border: none; background: transparent; }}
    #hints {{ padding: 0 2; color: $text-muted; }}
    """

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.busy = False
        self.pending: asyncio.Future | None = None  # an open question to the user; the next line answers it

    def compose(self) -> ComposeResult:
        log = VerticalScroll(id="log")
        log.can_focus = False
        with log:
            yield Static(
                f"[b {GRADIENT[1]}]✻[/] [b]OrbitRows[/]  [dim]·  {self.root.resolve()}[/]",
                id="banner",
            )
            yield message(
                "Start with your [b]store export[/]: type its path below. Then add a supplier file, "
                "or ask for changes in plain words. You can do both in one line."
            )
        with Vertical(id="prompt"):
            with Horizontal(id="box"):
                yield Static(">", id="caret")
                entry = Input(placeholder="data/store.csv, or a request like: raise all prices by 5%", id="entry")
                yield entry
            yield Static("tab complete · enter send · /preview /undo /merge /reset · esc quit", id="hints")
        yield CsvPathAutoComplete(
            entry, path=self.root, show_dotfiles=False,
            folder_prefix=Content("▸ "), file_prefix=Content("  "),
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.submit(event.value)

    def say(self, *widgets: Static | DataTable) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount_all(widgets)
        log.scroll_end(animate=False)

    def refuse(self, text: str) -> None:
        self.say(message(text, "✗", "red"))

    def submit(self, value: str) -> None:
        if not value.strip():
            return
        if self.pending and not self.pending.done():  # the line answers the open question
            self.query_one("#entry", Input).clear()
            self.say(message(f"[dim]{escape(value)}[/]", ">", "$text-muted"))
            self.pending.set_result(value.strip())
            return
        if self.busy:
            self.refuse("Still working on your last request.")
            return
        self.query_one("#entry", Input).clear()
        self.say(message(f"[dim]{escape(value)}[/]", ">", "$text-muted"))
        self.busy = True
        self.run_worker(self.handle(value.strip()))

    async def handle(self, value: str) -> None:
        try:
            if value.startswith("/"):
                await self.command(value.split())
            else:
                await self.route(*split_input(value, self.root))
        except Refusal as e:
            self.refuse(str(e))
        except OperationalError:
            self.refuse("Can't reach the database. Check DATABASE_URL in .env and that PostgreSQL is running.")
        except TypeSafeError as e:
            self.refuse(f"Jev couldn't answer ({type(e).__name__}). Nothing was changed; try again.")
        finally:
            self.busy = False

    async def route(self, prompt: str, path: Path | None) -> None:
        """The cases: no store yet → the file becomes the store; with a store → it becomes the incoming."""
        if path is None and self.app.store_id is None:
            raise Refusal("Upload a store first: type the path of your store CSV export.")
        if path is not None:
            await self.upload(path)
        if prompt:
            await self.ask(prompt)

    async def upload(self, path: Path) -> None:
        if not path.is_file():
            raise Refusal(f"File not found: {path}")
        try:
            header, rows = load_csv(path)
        except ValueError as e:
            raise Refusal(f"Cannot read {e}") from None
        need("DATABASE_URL")
        app = self.app
        if app.store_id is None:
            side, app.store_id = "store", await self.upload_store(path, header, rows)
        else:
            side, app.incoming_id = "incoming", await asyncio.to_thread(
                workspace.upload_incoming, app.store_id, header, rows
            )
        app.files[side] = {"name": path.name, "columns": header}
        columns = "  ".join(f"[{GRADIENT[i % len(GRADIENT)]}]{h or '(empty)'}[/]" for i, h in enumerate(header))
        self.say(message(
            f"Loaded [b]{path.name}[/] as the [b]{side}[/]  [dim]{len(rows)} rows · {len(header)} columns[/]\n  {columns}",
            "✓", GRADIENT[0],
        ))

    async def upload_store(self, path: Path, header: list[str], rows: list[list[str]]) -> int:
        """Column types + context (Jev) → user settles conflicts → one DB transaction."""
        need("OPENROUTER_API_KEY")
        step = lambda text: self.say(message(f"[dim]{text}…[/]", "◌", "$text-muted"))
        context, conflicts = await classify_columns(self.app.jev, header, rows, step=step)
        name = lambda key: header[int(key[1:]) - 1] or f"column {key[1:]}"
        for conflict in conflicts:
            options = [f"{name(k)}  [dim](Jev {p:.0%})[/]" for k, p in conflict.options] + ["none"]
            pick = await self.question(f"[b]{name(conflict.key)}[/]: which column gives its context?", options)
            if pick < len(conflict.options):
                context[conflict.key].client = conflict.options[pick][0]
        step("Saving")
        store_id = await asyncio.to_thread(workspace.upload_store, header, rows, context)
        self.say(message(context_summary(context, name), "◇", GRADIENT[2]))
        return store_id

    async def question(self, text: str, options: list[str]) -> int:
        """Ask in the log; the next submitted line answers. Returns the 0-based option."""
        listing = "\n".join(f"  [b]{i}[/]  {option}" for i, option in enumerate(options, 1))
        while True:
            self.say(message(f"{text}\n{listing}", "?", GRADIENT[1]))
            self.pending = asyncio.get_running_loop().create_future()
            try:
                answer = await self.pending
            finally:
                self.pending = None
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.refuse(f"Type a number from 1 to {len(options)}.")

    async def ask(self, prompt: str) -> None:
        need("OPENROUTER_API_KEY")
        intent = await classify(self.app.jev, prompt, self.app.files)
        targets = [side for side in ("store", "incoming") if getattr(intent, side)]
        if intent.command:  # a command asked in words; checked before on_topic, which judges the data only
            words = ["/" + intent.command]
            if intent.command == "undo":
                if len(targets) != 1:
                    raise Refusal("Undo which file? Say the store or the incoming file, or use /undo store or /undo incoming.")
                words += targets
            self.say(message(f"[dim]Running[/] [b]{' '.join(words)}[/]", "↳", "$text-muted"))
            await self.command(words)
            return
        if not intent.on_topic:
            hint = "" if self.app.incoming_id else " If it's about a supplier file, load that file first by typing its path."
            raise Refusal(
                "Your CSV files can't answer that, so nothing was changed. Ask about your data or change it, "
                f"or use a command: /preview /undo /merge /reset.{hint}"
            )
        if not targets:
            raise Refusal("Jev couldn't tell which file this is for. Say whether you mean the store or the incoming file.")
        # ponytail: prompt → Mongo update execution not built yet; this is where it plugs in
        self.say(message(
            f"This request is for the [b]{' and the '.join(targets)}[/].\n"
            "  [dim]Running prompts isn't built yet, so nothing was changed.[/]"
        ))

    async def command(self, words: list[str]) -> None:
        app = self.app
        match words:
            case ["/preview"]:
                store_id = self.require_store()
                headers, rows, total = await asyncio.to_thread(workspace.current, "store", store_id, PREVIEW_ROWS)
                table = DataTable(show_cursor=False, zebra_stripes=True, classes="preview")
                table.add_columns(*(h or f"({i})" for i, h in enumerate(headers[:PREVIEW_COLUMNS], 1)))
                table.add_rows(row[:PREVIEW_COLUMNS] for row in rows)
                count = lambda shown, total, what: f"{shown} of {total} {what}" if total > shown else f"{total} {what}"
                self.say(message(
                    "Latest store version  [dim]"
                    f"{count(len(rows), total, 'rows')} · {count(min(len(headers), PREVIEW_COLUMNS), len(headers), 'columns')}[/]"
                ), table)
            case ["/undo", "store" | "incoming" as side]:
                store_id = self.require_store()
                if side == "incoming" and app.incoming_id is None:
                    raise Refusal("There's no incoming file yet.")
                # One timeline for the session (doc §6): only the latest edit can be undone.
                result = await asyncio.to_thread(workspace.undo, store_id, side)
                other = "incoming" if side == "store" else "store"
                self.say({
                    "undone": message(f"Undid the last change to the {side}.", "↶"),
                    "nothing": message(f"Nothing to undo on the {side}.", "·", "$text-muted"),
                    "other_side": message(
                        f"The last change was on the {other} file. Changes are undone in order: /undo {other} first.",
                        "·", "$text-muted",
                    ),
                }[result])
            case ["/undo", *_]:
                raise Refusal("Say which file: /undo store or /undo incoming.")
            case ["/merge"]:
                self.require_store()
                if app.incoming_id is None:
                    raise Refusal("Upload an incoming file first: type the path of the supplier CSV.")
                # ponytail: merge pipeline (src/pipeline) not built yet
                self.say(message("Merge isn't built yet, so nothing was changed.", "·", "$text-muted"))
            case ["/reset"]:
                removed = await asyncio.to_thread(workspace.reset, self.require_store())
                self.say(message(f"Back to the files as uploaded ({removed} changes removed).", "↶"))
            case _:
                raise Refusal(f"Unknown command {words[0]}. Available commands:\n{COMMANDS}")

    def require_store(self) -> int:
        if self.app.store_id is None:
            raise Refusal("Upload a store first: type the path of your store CSV export.")
        return self.app.store_id
