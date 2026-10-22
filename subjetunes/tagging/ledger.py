# subjetunes/tagging/ledger.py
"""Remember what we wrote, so edits you make in rekordbox come back as corrections.

Round trip:
  1. Subjetunes writes {st #e4 #acid} and records ['#e4', '#acid'] in the ledger.
  2. In rekordbox you change the comment to {st #e4 #hypnotic}.
  3. Next run, read_back() sees +#hypnotic, -#acid and stores them as overrides,
     so the next suggestion keeps #hypnotic and never re-adds #acid. These are also
     good labels for the Criteria/Benchmark pages.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

from .comments import BlockFormat, normalise_path, read_tags
from .suggest import Override


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.written: dict[str, list[str]] = data.get("written", {})
        self.overrides: dict[str, Override] = {k: Override(**v) for k, v in data.get("overrides", {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"written": self.written,
                                   "overrides": {k: asdict(v) for k, v in self.overrides.items()}},
                                  indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, new_tags: Mapping[str, list[str]]) -> None:
        for k, tags in new_tags.items():
            self.written[normalise_path(k)] = list(tags)

    def set_override(self, key: str, add: list[str] = (), remove: list[str] = ()) -> None:
        k = normalise_path(key)
        ov = self.overrides.setdefault(k, Override())
        for t in add:
            if t in ov.remove:
                ov.remove.remove(t)
            if t not in ov.add:
                ov.add.append(t)
        for t in remove:
            if t in ov.add:
                ov.add.remove(t)
            if t not in ov.remove:
                ov.remove.append(t)

    def read_back(self, comments: Mapping[str, str], fmt: BlockFormat = BlockFormat()) -> dict[str, Override]:
        """Compare current Comments ({path: comment}) with what we last wrote.

        Only tracks we have written before are compared; returns the edits found and
        folds them into self.overrides.
        """
        found: dict[str, Override] = {}
        for raw, comment in comments.items():
            k = normalise_path(raw)
            if k not in self.written:
                continue
            now, before = read_tags(comment, fmt), self.written[k]
            add = [t for t in now if t not in before]
            rem = [t for t in before if t not in now]
            if add or rem:
                found[k] = Override(add=add, remove=rem)
                self.set_override(k, add, rem)
                self.written[k] = now
        return found
