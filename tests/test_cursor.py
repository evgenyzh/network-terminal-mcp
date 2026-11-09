"""Cursor tracking for opt-in DSR replies."""

from network_terminal_mcp.cursor import CursorTracker


def test_fragmented_queries_cursor_motion_and_restore() -> None:
    replies: list[str] = []
    cursor = CursorTracker(24, 120, replies.append)
    for chunk in ("hello\x1b[", "6", "n\x1b[s\x1b[9999;", "9999H\x1b[6n", "\x1b[u\x1b[6n"):
        cursor.feed(chunk)
    assert replies == ["\x1b[1;6R", "\x1b[24;120R", "\x1b[1;6R"]


def test_cursor_ignores_queries_inside_control_strings_and_other_csi() -> None:
    replies: list[str] = []
    cursor = CursorTracker(24, 120, replies.append)
    cursor.feed("\x1b]title \x1b[6n\x07\x1bPdata \x1b[6n\x1b\\")
    cursor.feed("\x1b[5n\x1b[?6n\x1b[c\x1b[>c\x1b[6n")
    assert replies == ["\x1b[1;1R"]


def test_unicode_and_pending_wrap() -> None:
    replies: list[str] = []
    cursor = CursorTracker(3, 4, replies.append)
    cursor.feed("a界b\x1b[6nX\x1b[6n\r\n\x1b[6n")
    assert replies == ["\x1b[1;4R", "\x1b[2;2R", "\x1b[3;1R"]
