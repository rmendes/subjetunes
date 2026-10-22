# Subjetunes

A desktop app for your rekordbox library (rekordbox 6 and 7, Windows and macOS). It analyses tracks,
suggests hashtag tags for the Comments field and sorts tracks into bucket playlists, then writes everything
back to rekordbox after you've reviewed it. You stay in charge: nothing is written until you hold Write,
and every write makes a backup first.

- **Analysis:** energy, swing and looseness from the audio (numpy DSP), and mood and instruments from CLAP
  (audio and text embeddings, CPU). Your own criteria, such as "hypnotic", are judged by a probe trained on
  your labels, by CLAP, or by a local LLM.
- **Tags:** a fixed vocabulary on separate axes (mood, movement, character, sound, arc, context), with a cap
  per axis and no contradictions on one track. Uncertain tags stay suggestions. Details are in `TAGGING.md`.
- **The app:** Library, then Batch, then Tag, then Write. The BPM × energy map, Camelot wheel, bucket lanes,
  HI/MID/LOW meters and the rest are described in `DESKTOP.md`.

## Run from source

Windows (PowerShell, Python 3.12):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cpu   # for CLAP
.\.venv\Scripts\python -m pip install -r requirements-ml.txt                                  # for CLAP
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python scripts\dev.py
```

macOS: the same, with `python3.12 -m venv .venv`, `.venv/bin/python …`, and plain `pip install torch`.

`scripts/dev.py` runs the app, restarts it when Python files change, reloads the window when web files
change, and keeps the portable build up to date in the background. Use `python -m subjetunes.desktop`
to run the app on its own.

## Builds for testers

| | Locally | In CI (every push to `main`) |
| --- | --- | --- |
| Windows | `packaging\build_portable.ps1` makes `dist\Subjetunes\` and a zip | `windows` job |
| macOS | `bash packaging/macos/build_macos.sh` makes `Subjetunes.app` and a zip/dmg | `macos` jobs: Apple silicon and Intel |

CI builds are under Actions > the run > Artifacts. Tag a version (`git tag v0.9.1 && git push --tags`)
to get a pre-release with all three. Mac testers follow `packaging/macos/README-testers.txt`.

## Where things are

```
subjetunes/            analysis engine (cli, pipeline, scoring, features/, judges/, cache, rbxml, …)
subjetunes/desktop/    the app: local server, web UI (web/), library + master.db, sources, locator
subjetunes/tagging/    comment tags: vocabulary engine, Comments block, ledger, master.db / XML writers
config/                default.yaml (engine), tags.yaml (tag vocabulary), criteria.yaml, models.yaml
packaging/             Windows portable build + launcher, macos/ (app bundle, signing, tester notes)
scripts/dev.py         development loop
tests/                 pytest; tests/test_platforms.py simulates Windows and macOS library setups
```

Your data (cache, ledger, backups, logs, `user.yaml`) is never in the repository. From source it's in
`data/`; in the Windows build it's in `data\` next to the exe; on macOS it's in
`~/Library/Application Support/Subjetunes`.
