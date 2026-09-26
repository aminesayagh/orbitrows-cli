from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static


class OrbitRowsApp(App):
    TITLE = "OrbitRows"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Load a store CSV and an incoming CSV to start.", id="status")
        yield Footer()


def main() -> None:
    OrbitRowsApp().run()
