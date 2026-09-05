"""Release-time compatibility pins shared by the Python launcher."""

TUI_PACKAGE = "@woltspace/tui"
TUI_VERSION = "0.2.2"
TUI_BINARY = "woltspace-tui"
TUI_SERVICE_BINARY = "woltspace-tui-service"


def tui_spec() -> str:
    return f"{TUI_PACKAGE}@{TUI_VERSION}"
