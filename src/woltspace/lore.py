"""The lodge's voice — the colour, creatures, and lines the CLI speaks.

Every phrase here is recycled from the launcher the colony grew up with: the
amber headline with a green lore line tucked under it, the beaver beside the
URL, the tree that opens a path to the outside. The native CLI inherited the
behaviour and lost the voice; this module hands it back.

Two rules keep it safe to use everywhere:

* **Style, never restructure.** A styled line carries the same words as the
  plain one it replaces, so anything reading `woltspace status` still parses.
* **Colour is a courtesy.** `rich` drops every escape when stdout is not a
  terminal (and honours ``NO_COLOR``/``TERM=dumb``), so a piped run is plain
  ASCII text. If `rich` is missing entirely we print the same words with no
  colour at all rather than failing.
"""

from __future__ import annotations

# --- Palette -----------------------------------------------------------------
# The lodge at dusk: amber for what happened, moss for what it means, teal for
# anywhere you can actually go, terra for a broken dam.

AMBER = "#d99e42"      # headlines — the technical fact
MOSS = "#7fa650"       # the lore line under it
TEAL = "#3fb0a0"       # URLs and paths you can dial
TERRA = "#c05c4a"      # failures
BARK = "#8a7f6d"       # quiet detail, field labels
WORDMARK = f"bold {MOSS}"

# --- Creatures ---------------------------------------------------------------
# The bash launcher's own set. 🦫 is the colony; the rest are weather and wood.

BEAVER = "🦫"
TENT = "⛺"            # the lodge, pitched and open
MOON = "🌙"            # closed for the night
SUN = "☀️"             # waking up
TREE = "🌲"            # the tunnel out
TIMBER = "🪵"          # a plain note
BRICKS = "🧱"          # it broke
SLEEP = "💤"           # nothing running
ELEPHANT = "🐘"        # the one who remembers — backups
TRACKS = "🐾"          # reading the ground — doctor

#: State transitions, in the launcher's words. `(emoji, headline, subtitle)`.
#: The headlines say "the lodge" where the launcher said "container": there is
#: no container in a native run, and the lodge is what was ever meant.
TRANSITIONS = {
    "started": (TENT, "the lodge started", "the lodge is open"),
    "running": (BEAVER, "the lodge is running", "already gnawing"),
    "waking": (SUN, "the lodge is waking", "waking up the lodge..."),
    "stopped": (MOON, "the lodge stopped", "lodge closed for the night"),
    "quiet": (SLEEP, "the lodge is not running", "the lodge is quiet"),
    "broken": (BRICKS, "the lodge failed to open", "the dam broke"),
}

INDENT = "  "
#: Where a subtitle sits: under its headline, past the emoji.
SUBINDENT = "     "

# The two warnings the launcher never let you miss. Single dash, on purpose.
SHARE_WARNING = "share carefully - anyone with this link can reach your wolts."
TUNNEL_DIGGING = "tunnel still digging - the lodge keeps trying"


# --- Console -----------------------------------------------------------------


class _PlainConsole:
    """What we print with when `rich` is not installed: the words, no colour."""

    is_terminal = False

    def print(self, text: str = "", *, style: str | None = None) -> None:
        print(text)


def _build_console():
    try:
        from rich.console import Console
    except ImportError:  # pragma: no cover — rich is a declared dependency
        return _PlainConsole()
    # highlight=False: rich's auto-highlighter repaints numbers and paths in
    # colours of its own choosing, which fights the palette above. soft_wrap
    # keeps a long URL on one line so it stays copy-pasteable.
    return Console(highlight=False, soft_wrap=True, emoji=False)


_console = None


def console():
    global _console
    if _console is None:
        _console = _build_console()
    return _console


def _text(content: str, style: str | None = None):
    """A styled fragment, built as an object rather than as markup.

    Paths and URLs go through here, and a wolts dir called `~/wolts[old]`
    would be read as markup if these were format strings. Text objects have no
    such surface.
    """
    try:
        from rich.text import Text
    except ImportError:  # pragma: no cover
        return content
    return Text(content, style=style or "")


def _emit(fragment) -> None:
    console().print(fragment)


# --- Voices ------------------------------------------------------------------


def blank() -> None:
    _emit("")


def banner(title: str = "woltspace") -> None:
    """The wordmark: a beaver, two spaces, the name in bold moss."""
    blank()
    line = _text(f"{INDENT}{BEAVER}  ")
    try:
        line.append(title, style=WORDMARK)
        _emit(line)
    except AttributeError:  # pragma: no cover — plain fallback
        _emit(f"{INDENT}{BEAVER}  {title}")
    blank()


def headline(emoji: str, text: str, *, style: str = AMBER) -> None:
    """Emoji plus the amber fact — the top half of a lodge line."""
    line = _text(f"{INDENT}{emoji} ")
    try:
        line.append(text, style=style)
        _emit(line)
    except AttributeError:  # pragma: no cover
        _emit(f"{INDENT}{emoji} {text}")


def subtitle(text: str) -> None:
    """The moss-green lore line, tucked under its headline."""
    _emit(_text(f"{SUBINDENT}{text}", MOSS))


def transition(key: str, *, subtitle_override: str = "") -> None:
    """One of the lodge's known state changes, spoken the way it always was."""
    emoji, head, lore = TRANSITIONS[key]
    headline(emoji, head)
    said = subtitle_override or lore
    if said:
        subtitle(said)


def failure(text: str, *, subtitle_text: str = "the dam broke") -> None:
    headline(BRICKS, text, style=TERRA)
    if subtitle_text:
        subtitle(subtitle_text)


def link(url: str, *, emoji: str = BEAVER, note: str = "") -> None:
    """A dialable address: teal, beside a beaver, with its lore underneath."""
    line = _text(f"{INDENT}{emoji} ")
    try:
        line.append(url, style=TEAL)
        _emit(line)
    except AttributeError:  # pragma: no cover
        _emit(f"{INDENT}{emoji} {url}")
    if note:
        subtitle(note)


def labelled(label: str, value: str, *, value_style: str = TEAL) -> None:
    """`public: https://…` — a bark label, a teal value."""
    line = _text(f"{INDENT}")
    try:
        line.append(f"{label}: ", style=BARK)
        line.append(value, style=value_style)
        _emit(line)
    except AttributeError:  # pragma: no cover
        _emit(f"{INDENT}{label}: {value}")


def note(text: str, *, emoji: str = TIMBER, style: str = BARK) -> None:
    """A quiet aside — where the wolts live, where the logs go."""
    line = _text(f"{INDENT}{emoji} ")
    try:
        line.append(text, style=style)
        _emit(line)
    except AttributeError:  # pragma: no cover
        _emit(f"{INDENT}{emoji} {text}")


def warn(text: str) -> None:
    _emit(_text(f"{INDENT}{text}", AMBER))


def field(label: str, value: str, *, value_style: str = "") -> None:
    """`key: value`, coloured but unmoved.

    `woltspace status` is read by scripts, so this styles the exact line the
    plain CLI printed — same words, same order, no indent, nothing added.
    """
    line = _text("")
    try:
        line.append(f"{label}: ", style=AMBER)
        line.append(value, style=value_style)
        _emit(line)
    except AttributeError:  # pragma: no cover
        _emit(f"{label}: {value}")


def plain(text: str, style: str = "") -> None:
    _emit(_text(text, style) if style else text)


# --- Tunnel ------------------------------------------------------------------


def public_tunnel_lines(tunnel: dict) -> None:
    """Say where the outside world reaches this lodge, or that it cannot.

    `tunnel` is what `lifecycle.tunnel_report` resolved: whether publishing is
    on, and the URL if one is known.
    """
    if not tunnel.get("enabled"):
        blank()
        headline(TREE, "tunnel disabled - the lodge stays on this machine")
        return
    blank()
    headline(TREE, "tunnel open" if tunnel.get("url") else "starting tunnel")
    subtitle("opening a path to the outside")
    url = tunnel.get("url")
    if url:
        labelled("public", url)
        warn(SHARE_WARNING)
    else:
        warn(TUNNEL_DIGGING)
