"""What can travel as a voice note. Shared by the notify command and the lodge."""

from pathlib import Path

# Telegram shows these as a voice message; any other audio it may deliver as a
# file. The type is read from the name: the lists Python keeps differ by version.
VOICE_CONTENT_TYPES = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}
VOICE_SUFFIXES_TEXT = ".ogg, .oga, .opus, .mp3 or .m4a"

# The most text the speech service reads in one request.
SPEECH_MAX_CHARS = 4096


def voice_content_type(filename: str) -> str | None:
    """The audio type of a file that can be a voice note, or None if it cannot."""
    return VOICE_CONTENT_TYPES.get(Path(filename).suffix.lower())
