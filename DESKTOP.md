# Subjetunes desktop app

A native window (WebView2 through pywebview) over a small local API (`subjetunes/desktop/server.py`,
stdlib only, 127.0.0.1 with a per-run token). The front end is three files in
`subjetunes/desktop/web/`: no framework, no build step, nothing loaded from the internet.

## Your rekordbox library is the source

Subjetunes opens **master.db** with pyrekordbox (it finds it on its own; Settings takes another path,
and a collection XML still works read-only).

- **Read, any time, rekordbox open or not:** playlists (smart ones too), title/artist/album/label/genre,
  BPM, key, length, rating, colour, play count, comments, My Tags, artwork and rekordbox's beat grid.
  Nothing rekordbox already measured is analysed again.
- **Write, with rekordbox closed:** every edit is *staged* first (`data\pending.json`), so you can work
  with rekordbox open. Write copies master.db to `data\backups\`, writes everything in one transaction
  (detail edits, tags into Comments, bucket playlists under `Subjetunes/<batch> (date)`), then reads
  every value back. pyrekordbox bumps rekordbox's change counters, so rekordbox sees normal edits.
  Nothing existing is deleted. To undo: close rekordbox, copy the backup files next to master.db.

## Batches

A batch is one or more playlists, or a single track (search in Library, Open), or a mix. Batch shows
each track with energy, buckets and tags; click one for the Track screen. Quick tag works through the
batch by keyboard.

## Track screen

- **Word cloud**: tags from every source, bigger when more sources agree. Click a word to make it a tag.
- **Tags for Comments**, **Buckets**: toggle.
- **Already on the track**: My Tags, genre and the words in your comment. These are *clues*.
- **Details**: rekordbox's fields as inputs. Under each one, pills show what other sources say (with
  which source); click one to stage it. Changed fields are outlined until written.
- **Sources**: which ones answered, how close the match was, and a link.

| Source | Setup |
| --- | --- |
| rekordbox | always on |
| File tags | built in (mutagen) |
| MusicBrainz | nothing, rate-limited to 1 request/s |
| Last.fm | free API key from last.fm/api |
| Discogs | personal access token (Discogs settings, Developers) |
| SoundCloud | app client id and secret |
| beets | path to beets `library.db` |
| Lexicon | Lexicon running, Settings > Integrations > Local API on |
| Beatport | experimental, off by default: reads their public search page, like One Tagger, and can break when the site changes |

Online answers are cached in `data\sources.sqlite` (30 days; errors retried after an hour).
"Look up again" refreshes one track.

## Clues

Existing tags inform new ones (`subjetunes/tagging/clues.py`):

1. Rules can use them: `"dubstep" in clues and energy > 0.6`. Also available: `camelot`, `minor`,
   `rating`, `year`, `play_count`, `genre`.
2. A My Tag, genre or comment word that *is* one of your tags (My Tag "Acid" and `#acid`) is suggested.
3. Learned: from every track you've tagged, Subjetunes counts which tags come with which clues. If 4 of
   5 "uk-bass" tracks carry `#dark`, a new "uk-bass" track gets `#dark?` to confirm, with that reason
   shown. It gets better as you tag.

Tags you already typed in your own comment aren't duplicated in the `{st …}` block.

## Run from source while developing

```powershell
.\.venv\Scripts\python scripts\dev.py
```

This runs the app from your repo and keeps `dist\Subjetunes\` in step with it:

- A change to a `.py` file restarts the app. The window comes back on the screen you were on.
- A change in `subjetunes\desktop\web\` reloads the window in place.
- After every change, once you pause, `build_portable.ps1 -Quick` runs in the background and prints one
  status line. A sync takes seconds, because the bundled Python and its packages are reused unless the
  Python version or a requirements file changed; then the runtime is rebuilt once. The full output goes to
  `dist\build.log`. Edits made during a build are picked up by one more build right after it.
- Close the window or press Ctrl+C to stop. `--no-build` skips the portable sync, `--browser` opens a tab.

Plain `python -m subjetunes.desktop` still works for running the app on its own.

## Portable build (Windows)

    powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1            # with CLAP (torch CPU)
    powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -Lite      # without torch
    powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -WithFFmpeg

Output: `dist\Subjetunes\` with `Subjetunes.exe`, plus a zip. Your data stays in `data\`; to update,
replace everything else.

A rebuild never deletes `dist\Subjetunes\data\` or `config\user.yaml`. Only the runtime and the app
code are replaced. The zip leaves `data\` out too.

## Connecting the analysis engine

Analyse writes the batch to `data\batch\batch.xml` (AverageBpm and Tonality from rekordbox) and
`data\batch\batch.json` (path, bpm, key, length and rekordbox's beat grid per track; its path is in
the `SUBJETUNES_BATCH` environment variable), then runs
`python -m subjetunes.cli analyze --xml data\batch\batch.xml --playlist "Subjetunes batch"`.
The engine should take BPM and beat grid from there instead of measuring them.

Scores come back through one function the engine still needs:
`subjetunes.pipeline.score_rows(paths) -> list[dict]` (one flat dict per track: `path`, `energy`,
`looseness`, `comp_*`, `mood_*`, `crit_*`, `crit_*_conf`). Until then: `data\scores.csv` or demo scores.

## Design system

Tokens are at the top of `web/styles.css`.

**Look: a mixer faceplate.** Matte graphite in dark mode, brushed aluminium in light mode. A switched-on
control inverts its ink, the way a lit button looks on hardware. There's no brand accent and no glow.
Bahnschrift, the DIN face that ships with Windows, is used for headings, legends and readouts.

**Colour has three jobs and no more:**
- the energy ramp (`--e1` blue to `--e5` crimson)
- one cue-amber LED (`--led`), which always means "staged, not written yet"
- the meter segments (green, amber, red), only on the HI/MID/LOW meters

**Few words on screen.** Explanations live in hover tips (`data-tip`). Hold Alt to see every tag's key.

**Flow:** Library → Batch → Tag → Write. Settings holds sources, buckets and vocabulary. Inside a screen,
secondary things are folds (`details.fold`) that show a short summary when closed, rather than cards or tabs.

**Components (adapted from your Forever Components picks):**
- Batch: the BPM × energy dot map (drag to filter, click a dot to open it) and the Camelot wheel (click to
  filter; hover a key and the keys that mix with it are dashed).
- Bucket lanes: drag a track between buckets; Ctrl or Alt copies instead of moving.
- Tag:
  - The energy knob has six detents (0 = none, 1–5). Drag, scroll or use the keys.
  - BPM and key flip like a split-flap board when you change track.
  - The rating stars are staged, then written like any other detail.
  - The HI/MID/LOW meters follow the audio as it plays. Pioneer doesn't publish DJM crossover points,
    so the splits (250 Hz, 2.5 kHz) are `XOVER` at the top of `app.js`.
- Write: hold the ring for about a second to write. The receipt gets a WRITTEN stamp once every value
  has been read back.

**Keys:**
- Ctrl K: find anything.
- Batch: ↑↓ or J K move, Enter opens the track, F focus, L list or lanes.
- Tag: letters toggle tags, 0–5 set energy, ↑↓ previous or next track, Backspace undoes this track's
  tag edits, Esc goes back to the batch.
- Everywhere: Space plays from 40%, ← → jump 10 s.

## macOS builds

`packaging/macos/build_macos.sh` builds `Subjetunes.app` with its own Python (python-build-standalone,
pinned and checksum-verified), the same app code and the same dependencies. It produces a zip, plus a dmg
with `--dmg`, that holds the app and `README-testers.txt`.

- **Apple silicon:** the full build, with CLAP.
- **Intel:** always the lite build, because PyTorch no longer publishes Intel-Mac wheels.
- **On a Mac:** `scripts/dev.py` keeps the app in step with `build_macos.sh --quick`, the same way it does on Windows.

**For testers, without owning a Mac:** push to GitHub. `.github/workflows/build.yml` runs the tests, then
builds Windows, Apple-silicon Mac and Intel Mac side by side. The zips are under Actions > the run >
Artifacts. A tag such as `v0.3.0` also makes a pre-release with all of them attached. `dist/` is
gitignored, and the engine modules have to be committed for CI to include analysis.

| | Windows | macOS |
| --- | --- | --- |
| App | `dist\Subjetunes\` with `Subjetunes.exe` | `Subjetunes.app` with a shell launcher in `Contents/MacOS` |
| Your data | `data\` next to the exe | `~/Library/Application Support/Subjetunes/` (the .app is read-only) |
| Library (master.db) | `%APPDATA%\Pioneer\rekordbox\master.db` | `~/Library/Pioneer/rekordbox/master.db` |
| Where the library is recorded | `%APPDATA%\Pioneer\rekordboxAgent\storage\options.json` | `~/Library/Application Support/Pioneer/rekordboxAgent/storage/options.json` |
| rekordbox app | `Program Files\rekordbox\rekordbox 7.x` (6: `Pioneer\`) | `/Applications/rekordbox 7/rekordbox.app`, or `~/Applications` |
| Database key | carried by pyrekordbox, the same on both | same |
| "Is rekordbox open?" | process named exactly `rekordbox` (rekordboxAgent doesn't count) | same |
| Shortcuts | Ctrl | ⌘ (the hints change automatically) |

**Finding the library.** Subjetunes reads where the library is from rekordbox's own files: first
`options.json`, then `rekordbox3.settings`, then the default location. Then it opens that file
directly. This works when rekordbox isn't in the default folder, when the library was moved, or when
those two files disagree. pyrekordbox's own lookup fails in all three cases. Settings > Library shows
every place it looked and which rekordbox versions are installed. That's the first thing to ask a tester
for when their library doesn't open.

**Signing.** By default the build is ad-hoc signed, which Apple silicon requires. Testers approve it once
in System Settings > Privacy & Security > Open Anyway; `README-testers.txt` walks them through it.

With an Apple Developer account you can sign and notarise instead, so the app opens with a double-click:
- **Locally:** set `SIGN_IDENTITY` and `NOTARY_PROFILE`.
- **In CI:** add the `MACOS_CERT_*` and `APPLE_*` secrets listed in the workflow.

**macOS asks before reading external or network drives.** `Info.plist` explains why, and testers have to
click Allow or tracks won't play or analyse. An ad-hoc signature changes with every build, so a new test
build may ask again.

**Keep code platform-neutral.** Use `pathlib`, never hard-code `C:\` or backslashes, keep data under
`paths.home()`, and test path-dependent code with `tests/test_platforms.py`, which simulates both systems
on any OS.

## The CLAP model (Hugging Face)

The first analysis with CLAP downloads `laion/larger_clap_music` (about 800 MB) into `data/models`. If you
had already downloaded it to the usual Hugging Face cache (`~/.cache/huggingface`), that copy is used
instead of a second download.

Once the model is on disk, Subjetunes works offline for it. Hugging Face doesn't check for updates on
every start, so there's no "unauthenticated requests" warning, startup is faster, and analysis works
without internet.

A token (`HF_TOKEN`) isn't needed. If you set one yourself, it's used for downloads. To check for a newer
version of the model, start once with `SUBJETUNES_HF_ONLINE=1`. Changing `clap.model_id` goes online
by itself the first time, because the new model isn't on disk yet. The log's first lines say where the
model is and whether the app is offline.
