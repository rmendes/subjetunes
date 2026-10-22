# Comment tags

Subjetunes suggests short hashtags per track and writes them into the rekordbox
**Comments** field, inside one block it owns:

    8A - Energy 6 | great with Foo {st #e4 #tight #acid #dark #b-peak-time}

Everything outside `{st ...}` is yours and is never changed. Re-running replaces only the block.
In rekordbox, show the Comments column, search `#acid`, or make smart playlists with
*Comments contains `#acid`*.

## Vocabulary

`config/tags.yaml` is a defined list organised by axis, so tagging stays consistent and searchable:

| Axis | Up to | Tags |
| --- | --- | --- |
| energy | 1 | `#e1`…`#e5` (quantiles of the batch) |
| mood | 2 | euphoric, uplifting, dreamy, melancholic, dark, tense, aggressive, hypnotic |
| movement | 1 | broken, groovy, driving, rolling, floating |
| character | 2 | analog, digital, raw, sparse |
| sound | 4 | vocal, instrumental, acid, piano, strings |
| arc | 1 | slow-burn, dynamic, steady (needs `energy_spread` / `energy_slope` from the engine) |
| context | 2 | peak, warmup, afterhours, closing |

How the suggestions are made:
- **Mood** comes from valence × arousal: how positive the track is, and how energetic the emotion is.
  When the engine doesn't provide `valence` and `arousal`, brightness and energy stand in.
- **Caps per axis.** Inside each axis the tags with the strongest evidence win, up to that axis's limit.
  This keeps a track to a small profile: one or two moods, an energy level, a movement, one or two contexts.
- **Conflicts.** Pairs such as euphoric and dark never sit on one track; the weaker evidence loses.
  A tag you add yourself always beats an automatic one it contradicts.
- **Choices and buckets.** Label criteria become `prefix + value` (`#vox-sung-hook`), and each bucket the
  track lands in becomes `#b-<id>`.

You can edit the list where you tag: double-click (or right-click) a tag to rename it, + adds a tag to
an axis, and an empty name removes it. Changes go to `data/user.yaml`. Renames carry over your edits and
what was already written, so the next Write updates the Comments field.

Run `python -m subjetunes.tagging.cli check --buckets <ids>` after editing the file by hand. It catches the
substring trap: rekordbox's "contains" would match `#warm` inside `#warmup`.

## Certainty

A rule tag is written only when it's **stable**: moving any one input by `margin` (0.05)
doesn't flip it, and any judge confidence (`<criterion>_conf`) is at least `min_confidence`.
Otherwise it's listed as *uncertain* for Review. Level tags are always written and flagged
*borderline* near a cut. Your edits (`add` / `remove`) always win and survive re-runs.

## Getting tags into rekordbox

**A. master.db (recommended, rekordbox closed)**

    python -m subjetunes.tagging.cli db --scores data\scores.csv          # dry run, shows diffs
    python -m subjetunes.tagging.cli db --scores data\scores.csv --write  # backup, write, verify

Backups go to `data\backups\master_<time>\`. Only `DjmdContent.Commnt` changes.

**B. XML (rekordbox can stay open)**

    python -m subjetunes.tagging.cli xml --scores data\scores.csv --source-xml C:\rb_fresh.xml

Use a *fresh* collection export as `--source-xml`. Then in rekordbox, in the
"rekordbox xml" tree, open **Subjetunes tag update**, select all **tracks** (Ctrl+A),
right-click, **Import To Collection**. Importing the playlist alone does not update tracks
that already exist. Each TRACK is copied verbatim from your export (beat grid and cues
included), so the forced import only changes Comments. Try it on 2–3 tracks first.

`--export data\exports\X.xml` puts the tags into a bucket export instead, so one file carries
both the playlists and the tags.

## Edits made in rekordbox come back

Change a block in rekordbox (say `#acid` to `#hypnotic`), export the collection, then:

    python -m subjetunes.tagging.cli readback --source-xml C:\rb_fresh.xml

The differences from what Subjetunes last wrote (`data\tag_ledger.json`) become overrides.
They are also labels you can use for the probe judges.

## Scores input

`--scores` is a CSV or JSON with one row per track: `path` (or `location`), optional `buckets`
(ids separated by `;`), and score columns (`energy`, `looseness`, `comp_vocals`, `crit_hypnotic`,
`crit_hypnotic_conf`, ...). This is a stopgap until the Review page calls
`subjetunes.tagging.suggest_batch()` directly.
