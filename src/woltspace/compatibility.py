"""Identity of the npm half, shared by the Python launcher.

The two artifacts release on their own cadence. The tui declares the minimum
`woltspace` version it needs and checks it at startup; the wheel does not check
the tui's version at all — it only checks that a binary on PATH really is the
`@woltspace/tui` bin it claims to be. So there is no version constant here, and
nothing to keep in lockstep.
"""

TUI_PACKAGE = "@woltspace/tui"
TUI_BINARY = "woltspace-tui"
TUI_SERVICE_BINARY = "woltspace-tui-service"


def tui_spec() -> str:
    """What the npx fallback resolves, and what the messages name.

    `latest`, not a pin: any published tui works with any wheel that satisfies
    the tui's own declared minimum.
    """
    return f"{TUI_PACKAGE}@latest"
