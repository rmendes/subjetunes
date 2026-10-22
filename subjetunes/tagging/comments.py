# subjetunes/tagging/comments.py
"""Read and write Subjetunes tags inside a track's Comments field.

Tags live in one delimited block so we can rewrite our own part idempotently and
never touch what you (or Mixed In Key, etc.) wrote:

    "8A - Energy 6 | great with Foo  {st #e4 #acid #hypnotic}"
     └─────── yours, kept as-is ───┘ └──── ours, replaced ───┘

In rekordbox, search the Comments column or build smart playlists with
"Comments contains #acid".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

TAG_RE = re.compile(r"^#[a-z0-9][a-z0-9_\-]*$")


@dataclass(frozen=True)
class BlockFormat:
    open: str = "{st"
    close: str = "}"
    position: str = "end"          # "end" | "start"
    max_len: int = 255             # warn above this; long comments get truncated on some players

    def pattern(self) -> re.Pattern[str]:
        return re.compile(r"\s*" + re.escape(self.open) + r"(?:\s[^" + re.escape(self.close) + r"]*)?"
                          + re.escape(self.close) + r"\s*")


def normalise_tag(raw: str) -> str:
    """'Acid Line' -> '#acid-line'. Raises ValueError if nothing usable is left."""
    s = unicodedata.normalize("NFKD", raw.strip()).encode("ascii", "ignore").decode().lower()
    s = s.lstrip("#")
    s = re.sub(r"[\s/]+", "-", s)
    s = re.sub(r"[^a-z0-9_\-]", "", s).strip("-")
    if not s:
        raise ValueError(f"tag {raw!r} has no usable characters")
    return "#" + s


def read_tags(comment: str, fmt: BlockFormat = BlockFormat()) -> list[str]:
    """Tags inside our block, in order. Empty list if there is no block."""
    m = fmt.pattern().search(comment or "")
    if not m:
        return []
    inner = m.group(0).strip()[len(fmt.open):-len(fmt.close)]
    return [t for t in inner.split() if TAG_RE.match(t)]


def user_text(comment: str, fmt: BlockFormat = BlockFormat()) -> str:
    """The comment with our block removed (what the user owns)."""
    return fmt.pattern().sub(" ", comment or "").strip()


def merge_comment(existing: str, tags: list[str], fmt: BlockFormat = BlockFormat()) -> str:
    """Replace our block in ``existing`` with ``tags``. No tags removes the block."""
    keep = user_text(existing, fmt)
    seen: set[str] = set()
    uniq = [t for t in tags if not (t in seen or seen.add(t))]
    if not uniq:
        return keep
    block = f"{fmt.open} {' '.join(uniq)}{fmt.close}"
    if not keep:
        return block
    return f"{block} {keep}" if fmt.position == "start" else f"{keep} {block}"


def normalise_path(location_or_path: str) -> str:
    """One key for the same file, whether it came from an XML Location or master.db FolderPath.

    'file://localhost/C:/Music/a%20b.mp3' and 'C:\\Music\\a b.mp3' -> 'c:/music/a b.mp3'.
    Windows paths are case-insensitive, so drive-letter paths are case-folded.
    """
    s = location_or_path or ""
    if s.startswith("file://"):
        s = s[len("file://"):]
        if s.startswith("localhost"):
            s = s[len("localhost"):]
        s = unquote(s)
    s = unicodedata.normalize("NFC", s.replace("\\", "/"))
    if re.match(r"^/[A-Za-z]:/", s):
        s = s[1:]
    if re.match(r"^[A-Za-z]:/", s):
        s = s.casefold()
    return s
