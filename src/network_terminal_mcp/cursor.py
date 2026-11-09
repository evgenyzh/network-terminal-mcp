"""Small, bounded cursor tracker for PTY DSR replies, not a terminal emulator."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable


class CursorTracker:
    """Track common cursor operations and answer CSI 6 n as output arrives.

    Unknown operations are ignored; complex screen modes, grapheme clusters and
    scrolling regions are intentionally not emulated. Do not use for rendering.
    """

    def __init__(self, rows: int, columns: int, reply: Callable[[str], None]) -> None:
        self.rows = rows
        self.columns = columns
        self.reply = reply
        self.row = self.column = 1
        self.saved = (1, 1)
        self.state = "text"
        self.csi = ""
        self.wrap_pending = False

    def feed(self, text: str) -> None:
        for char in text:
            self._feed_char(char)

    def _feed_char(self, char: str) -> None:
        if self.state == "string":
            if char == "\x07":
                self.state = "text"
            elif char == "\x1b":
                self.state = "string_escape"
            return
        if self.state == "string_escape":
            self.state = "text" if char == "\\" else "string"
            return
        if self.state == "escape":
            self.state = "text"
            if char == "[":
                self.state, self.csi = "csi", ""
            elif char in "]P^_X":
                self.state = "string"
            elif char == "7":
                self.saved = (self.row, self.column)
            elif char == "8":
                self.row, self.column = self.saved
                self.wrap_pending = False
            elif char == "c":
                self.row = self.column = 1
                self.wrap_pending = False
            elif char == "\x1b":
                self.state = "escape"
            return
        if self.state == "csi":
            if char == "\x1b":
                self.state = "escape"
            elif "@" <= char <= "~":
                self.state = "text"
                self._csi(char)
            elif len(self.csi) < 64 and " " <= char <= "?":
                self.csi += char
            else:
                self.state = "text"
            return
        if char == "\x1b":
            self.state = "escape"
        elif char == "\r":
            self.column, self.wrap_pending = 1, False
        elif char in "\n\x0b\x0c":
            self.row, self.wrap_pending = min(self.rows, self.row + 1), False
        elif char == "\b":
            self.column, self.wrap_pending = max(1, self.column - 1), False
        elif char == "\t":
            self.column = min(self.columns, ((self.column - 1) // 8 + 1) * 8 + 1)
            self.wrap_pending = False
        elif char >= " " and char != "\x7f":
            width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in "WF" else 1
            )
            if width:
                if self.wrap_pending or self.column + width - 1 > self.columns:
                    self.row, self.column = min(self.rows, self.row + 1), 1
                end = self.column + width
                self.wrap_pending = end > self.columns
                self.column = min(self.columns, end)

    def _csi(self, final: str) -> None:
        params = self.csi
        if final == "n" and params == "6":
            self.reply(f"\x1b[{self.row};{self.column}R")
            return
        if any(c not in "0123456789;" for c in params):
            return
        if len(params) > 32:
            return
        values = [int(value) if value else 0 for value in params.split(";")]
        first = values[0] or 1
        second = (values[1] if len(values) > 1 else 0) or 1
        if final in "Hf":
            self.row, self.column = min(self.rows, first), min(self.columns, second)
        elif final == "A":
            self.row = max(1, self.row - first)
        elif final == "B":
            self.row = min(self.rows, self.row + first)
        elif final == "C":
            self.column = min(self.columns, self.column + first)
        elif final == "D":
            self.column = max(1, self.column - first)
        elif final == "G":
            self.column = min(self.columns, first)
        elif final == "d":
            self.row = min(self.rows, first)
        elif final == "s" and not params:
            self.saved = (self.row, self.column)
        elif final == "u" and not params:
            self.row, self.column = self.saved
        else:
            return
        self.wrap_pending = False
