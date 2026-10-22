# subjetunes/tagging/cli.py
"""Comment tags from the command line.

    python -m subjetunes.tagging.cli check    [--buckets peak_time,warmup_groovy]
    python -m subjetunes.tagging.cli suggest  --scores data/scores.csv [--out data/tags.csv]
    python -m subjetunes.tagging.cli xml      --scores data/scores.csv --source-xml C:\\rb.xml [--export data\\exports\\X.xml]
    python -m subjetunes.tagging.cli db       --scores data/scores.csv [--write]
    python -m subjetunes.tagging.cli readback --source-xml C:\\rb_fresh.xml

--scores: CSV or JSON, one row per track. Needs a path column (`path`, `location` or `key`),
optional `buckets` (ids separated by ';'), and the score columns (energy, comp_vocals, ...).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from .comments import merge_comment
from .ledger import Ledger
from .suggest import DEFAULT_CONFIG_PATH, TagConfig, suggest_batch, validate

ROOT = DEFAULT_CONFIG_PATH.parents[1]


def _coerce(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s)
        except ValueError:
            return s if s else None
    return v


def load_scores(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
    else:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        key = r.get("key") or r.get("path") or r.get("location")
        if not key:
            raise SystemExit(f"{path}: every row needs a path, location or key column")
        b = r.get("buckets") or []
        if isinstance(b, str):
            b = [x.strip() for x in b.replace(",", ";").split(";") if x.strip()]
        row = {k: _coerce(v) for k, v in r.items() if k not in ("key", "path", "location", "buckets")}
        row.update(key=str(key), buckets=b)
        out.append(row)
    return out


def _config() -> TagConfig:
    return TagConfig.load(ROOT / "config" / "tags.yaml", ROOT / "config" / "user.yaml")


def _suggest(scores: Path, ledger: Ledger):
    cfg = _config()
    rows = load_scores(scores)
    problems = validate(cfg, {b for r in rows for b in r["buckets"]})
    for p in problems:
        print(f"warning: {p}", file=sys.stderr)
    return cfg, suggest_batch(rows, cfg, ledger.overrides)


def cmd_check(a: argparse.Namespace) -> int:
    probs = validate(_config(), [b for b in (a.buckets or "").split(",") if b])
    for p in probs:
        print(p)
    print("tag vocabulary OK" if not probs else f"{len(probs)} problem(s)")
    return 1 if probs else 0


def cmd_suggest(a: argparse.Namespace) -> int:
    ledger = Ledger(a.ledger)
    cfg, res = _suggest(a.scores, ledger)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with a.out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["path", "tags", "uncertain", "comment_block", "reasons"])
            for t in res:
                w.writerow([t.key, " ".join(t.tags), " ".join(t.uncertain), merge_comment("", t.tags, cfg.block),
                            "; ".join(f"{s.tag}: {s.reason}" for s in t.suggestions)])
        print(f"wrote {a.out}")
    else:
        for t in res:
            extra = f"   ?{' ?'.join(t.uncertain)}" if t.uncertain else ""
            print(f"{merge_comment('', t.tags, cfg.block):<60} {t.key}{extra}")
    n_rev = sum(t.needs_review for t in res)
    print(f"{len(res)} tracks, {n_rev} with uncertain tags (review them before writing)", file=sys.stderr)
    return 0


def cmd_xml(a: argparse.Namespace) -> int:
    from .xml_update import apply_to_export, write_comment_update_xml
    ledger = Ledger(a.ledger)
    cfg, res = _suggest(a.scores, ledger)
    tags = {t.key: t.tags for t in res}
    out = a.out or ROOT / "data" / "exports" / f"subjetunes_tags_{datetime.now():%Y%m%d_%H%M}.xml"
    if a.export:
        rep = apply_to_export(a.export, a.source_xml, tags, out, cfg.block)
    else:
        rep = write_comment_update_xml(a.source_xml, tags, out, cfg.block)
    ledger.record(tags)
    ledger.save()
    print(f"wrote {out}: {rep.changed} changed, {rep.unchanged} unchanged, {len(rep.not_found)} not in the XML")
    for k in rep.too_long[:5]:
        print(f"  comment longer than {cfg.block.max_len} chars: {k}")
    print("In rekordbox: open the playlist in the 'rekordbox xml' tree, select all TRACKS (Ctrl+A),\n"
          "right-click > Import To Collection. Importing only the playlist does not update existing tracks.")
    return 0


def cmd_db(a: argparse.Namespace) -> int:
    from .masterdb import write_comments
    ledger = Ledger(a.ledger)
    cfg, res = _suggest(a.scores, ledger)
    tags = {t.key: t.tags for t in res}
    rep = write_comments(tags, ROOT / "data" / "backups", a.db, a.key, cfg.block, dry_run=not a.write)
    for ch in rep.changes[: (len(rep.changes) if a.verbose else 10)]:
        print(f"{ch.key}\n   - {ch.old!r}\n   + {ch.new!r}")
    print(f"{len(rep.changes)} to change, {rep.unchanged} unchanged, {len(rep.not_found)} not in master.db")
    if rep.written:
        ledger.record({c.key: tags.get(c.key, []) for c in rep.changes})
        ledger.save()
        print(f"written. backup: {rep.backup_dir}")
    elif not a.write:
        print("dry run: nothing written. Close rekordbox and add --write to apply.")
    return 0


def cmd_readback(a: argparse.Namespace) -> int:
    ledger = Ledger(a.ledger)
    coll = ET.parse(a.source_xml).getroot().find("COLLECTION")
    comments = {t.get("Location", ""): t.get("Comments", "") for t in coll.findall("TRACK")} if coll is not None else {}
    found = ledger.read_back(comments, _config().block)
    ledger.save()
    for k, ov in found.items():
        print(f"{k}: +{' +'.join(ov.add) if ov.add else ''} -{' -'.join(ov.remove) if ov.remove else ''}")
    print(f"{len(found)} tracks edited in rekordbox; saved as overrides in {a.ledger}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="subjetunes.tagging")
    ap.add_argument("--ledger", type=Path, default=ROOT / "data" / "tag_ledger.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.add_argument("--buckets", default="")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("suggest"); p.add_argument("--scores", type=Path, required=True)
    p.add_argument("--out", type=Path); p.set_defaults(fn=cmd_suggest)
    p = sub.add_parser("xml"); p.add_argument("--scores", type=Path, required=True)
    p.add_argument("--source-xml", type=Path, required=True, help="fresh collection export from rekordbox")
    p.add_argument("--export", type=Path, help="add tags into this Subjetunes bucket export instead")
    p.add_argument("--out", type=Path); p.set_defaults(fn=cmd_xml)
    p = sub.add_parser("db"); p.add_argument("--scores", type=Path, required=True)
    p.add_argument("--db", type=Path); p.add_argument("--key", default="")
    p.add_argument("--write", action="store_true"); p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_db)
    p = sub.add_parser("readback"); p.add_argument("--source-xml", type=Path, required=True)
    p.set_defaults(fn=cmd_readback)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
