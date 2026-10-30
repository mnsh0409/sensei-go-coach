"""Go coordinates in GTP style.

(row, col) has row 0 at the TOP of the board, matching KataGo's ownership order.
sgfmill uses row 0 at the bottom; convert only at that boundary.
"""

from __future__ import annotations

import re

GO_COLUMNS = "ABCDEFGHJKLMNOPQRST"
PASS = "pass"

_COORD_RE = re.compile(r"(?<![A-Za-z0-9])([A-HJ-T])(1[0-9]|[1-9])(?![0-9])")


def gtp_to_rc(move: str, size: int = 19) -> tuple[int, int] | None:
    """'Q16' -> (row, col). 'pass' -> None. Raises ValueError for anything off the board."""
    m = move.strip().upper()
    if m == "PASS":
        return None
    if len(m) < 2:
        raise ValueError(f"bad coordinate: {move!r}")
    letter, number = m[0], m[1:]
    if letter not in GO_COLUMNS[:size] or not number.isdigit():
        raise ValueError(f"bad coordinate: {move!r}")
    n = int(number)
    if not 1 <= n <= size:
        raise ValueError(f"bad coordinate: {move!r}")
    return size - n, GO_COLUMNS.index(letter)


def rc_to_gtp(rc: tuple[int, int] | None, size: int = 19) -> str:
    """(row, col) -> 'Q16'. None -> 'pass'."""
    if rc is None:
        return PASS
    row, col = rc
    if not (0 <= row < size and 0 <= col < size):
        raise ValueError(f"off board: {rc!r}")
    return f"{GO_COLUMNS[col]}{size - row}"


def find_coords(text: str, size: int = 19) -> list[str]:
    """All on-board GTP coordinates mentioned in text, in order of appearance (uppercase only)."""
    found = []
    for letter, number in _COORD_RE.findall(text):
        if letter in GO_COLUMNS[:size] and 1 <= int(number) <= size:
            found.append(f"{letter}{number}")
    return found


def neighbors(rc: tuple[int, int], size: int = 19) -> list[tuple[int, int]]:
    row, col = rc
    out = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        r, c = row + dr, col + dc
        if 0 <= r < size and 0 <= c < size:
            out.append((r, c))
    return out


def region(rc: tuple[int, int] | None, size: int = 19) -> str:
    """'corner', 'edge' or 'center' by distance to the edges (line 1 to 4 counts as edge-side)."""
    if rc is None:
        return "pass"
    row, col = rc
    d_row = min(row, size - 1 - row)
    d_col = min(col, size - 1 - col)
    near_row, near_col = d_row <= 3, d_col <= 3
    if near_row and near_col:
        return "corner"
    if near_row or near_col:
        return "edge"
    return "center"
