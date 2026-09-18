"""Arrow-key picker for the interactive app. Standard library only (POSIX termios).

The state change is a pure function (`move`) so it can be tested without a terminal;
`read_key` and `run_picker` are the thin terminal layer around it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence

UP, DOWN, ENTER, BACK = "up", "down", "enter", "back"

ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
_CLEAR = "\033[H\033[2J"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"


def supported(stdin=None, stdout=None) -> bool:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if os.name != "posix":
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return stdin.isatty() and stdout.isatty()


def decode_key(seq: str) -> str:
    """Map raw input bytes to a key name. Unknown keys are returned as they came."""
    if seq in ("\x1b[A", "\x1bOA", "k"):
        return UP
    if seq in ("\x1b[B", "\x1bOB", "j"):
        return DOWN
    if seq in ("\r", "\n", " "):
        return ENTER
    if seq in ("\x1b", "q", "Q", "\x7f"):
        return BACK
    return seq


def read_key(fd: int | None = None) -> str:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno() if fd is None else fd
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        seq = os.read(fd, 1).decode(errors="ignore")
        if seq == "\x03":
            raise KeyboardInterrupt
        if seq == "\x1b":
            while select.select([fd], [], [], 0.03)[0]:
                seq += os.read(fd, 1).decode(errors="ignore")
                if len(seq) >= 3:
                    break
        return decode_key(seq)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def move(index: int, key: str, selectable: Sequence[bool]) -> int:
    """Next selected row after a key press. Skips rows that cannot be selected, wraps."""
    rows = [i for i, ok in enumerate(selectable) if ok]
    if not rows:
        return index
    if index not in rows:
        return rows[0]
    pos = rows.index(index)
    if key == UP:
        return rows[(pos - 1) % len(rows)]
    if key == DOWN:
        return rows[(pos + 1) % len(rows)]
    if key.isdigit() and key != "0" and int(key) <= len(rows):
        return rows[int(key) - 1]
    return index


def run_picker(
    render: Callable[[int], str],
    selectable: Sequence[bool],
    start: int = 0,
    out=None,
    key_source: Callable[[], str] = read_key,
) -> int | None:
    """Redraw `render(selected)` on every key. Enter returns the row, back returns None."""
    out = out or sys.stdout
    index = move(start, "", selectable)
    out.write(_HIDE_CURSOR)
    try:
        while True:
            out.write(_CLEAR + render(index))
            out.flush()
            key = key_source()
            if key == ENTER:
                return index
            if key == BACK:
                return None
            index = move(index, key, selectable)
    finally:
        out.write(_SHOW_CURSOR)
        out.flush()
