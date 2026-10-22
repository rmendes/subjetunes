Subjetunes for macOS : test build
=================================

1. Unzip, then drag Subjetunes.app into your Applications folder.
   (Run it from Applications, not from Downloads.)

2. First launch. This test build isn't notarised by Apple yet, so macOS blocks it once:
   - Double-click Subjetunes. macOS says it can't verify the developer. Click Done.
   - Open System Settings > Privacy & Security, scroll down, and click "Open Anyway" next to
     Subjetunes. Confirm with your password or Touch ID.
   After that it opens normally.
   Terminal alternative:  xattr -dr com.apple.quarantine /Applications/Subjetunes.app

3. Subjetunes finds your rekordbox library by itself (rekordbox 6 or 7). Reading works while
   rekordbox is open; writing waits until you close rekordbox. A backup of your library is made
   before every write, in ~/Library/Application Support/Subjetunes/data/backups.

4. If your music is on an external drive, macOS asks once whether Subjetunes may access it.
   Click Allow, or tracks won't play or analyse. Each new test build may ask again.

Which build?
   Apple silicon (M1 and later):  Subjetunes-...-macos-arm64.zip   (full, with CLAP mood analysis)
   Intel Mac:                     Subjetunes-...-macos-x86_64.zip  (lite: no CLAP, everything else works)

Reporting a problem: send
   ~/Library/Application Support/Subjetunes/data/logs/subjetunes.log
   and the "Library" section of Settings (where it looked for your library).
