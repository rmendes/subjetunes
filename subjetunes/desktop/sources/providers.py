# subjetunes/desktop/sources/providers.py
"""The sources. Each one is small on purpose: search, pick the closest match, normalise.

Local:  rekordbox (master.db), file tags (mutagen), beets (library.db), Lexicon (local API)
Online: MusicBrainz (no key), Last.fm (API key), Discogs (token), SoundCloud (client id + secret),
        Beatport (experimental: reads its public search page, may break; off by default)
"""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .base import Result, Source, TrackQuery, base_title, register, similarity, split_tags


@register
class Rekordbox(Source):
    id, name, online, trust = "rekordbox", "rekordbox", False, 1.3

    def lookup(self, q: TrackQuery) -> Result:
        m = dict(q.meta)
        tags = split_tags(m.get("genre", ""), 1.0)
        tags += [[t.split("/")[-1], 1.0] for t in q.mytags]
        comment = re.sub(r"\{st[^}]*\}", "", m.get("comments", "") or "")
        tags += [[h, 0.9] for h in re.findall(r"#([\w\-]+)", comment)]
        tags += split_tags(re.sub(r"#[\w\-]+", "", comment), 0.7)
        if m.get("color"):
            tags.append([m["color"], 0.3])
        fields = {k: m.get(k) for k in ("title", "artist", "album", "album_artist", "remixer", "mix_name",
                                        "original_artist", "composer", "label", "genre", "year", "release_date",
                                        "track_no", "disc_no", "isrc", "bpm", "key", "comments")}
        return self.result(state="found", fields=fields, tags=tags, match=1.0,
                           extra={"mytags": q.mytags, "play_count": m.get("play_count"), "rating": m.get("rating")})


@register
class FileTags(Source):
    id, name, online, trust = "file", "File tags", False, 1.0

    def lookup(self, q: TrackQuery) -> Result:
        try:
            import mutagen
        except ImportError:
            return self.result(state="off", note="mutagen isn't installed")
        p = Path(q.path)
        if not p.is_file():
            return self.result(state="error", note=f"File not found: {p}")
        f = mutagen.File(p, easy=False)
        if f is None or f.tags is None:
            return self.result(state="none", note="No tags in the file")
        raw = {}
        for k, v in f.tags.items():
            key = str(k).split(":")[0].upper()
            val = getattr(v, "text", v)
            val = val if isinstance(val, (list, tuple)) else [val]
            txt = "; ".join(str(x) for x in val if str(x).strip())
            if txt and len(txt) < 2000:
                raw.setdefault(key, txt)

        def pick(*names):
            for n in names:
                if raw.get(n.upper()):
                    return raw[n.upper()]
            return ""

        fields = {
            "title": pick("TIT2", "TITLE", "©NAM"), "artist": pick("TPE1", "ARTIST", "©ART"),
            "album": pick("TALB", "ALBUM", "©ALB"), "album_artist": pick("TPE2", "ALBUMARTIST", "AART"),
            "remixer": pick("TPE4", "REMIXER", "MIXARTIST"), "composer": pick("TCOM", "COMPOSER", "©WRT"),
            "label": pick("TPUB", "LABEL", "ORGANIZATION", "PUBLISHER"), "genre": pick("TCON", "GENRE", "©GEN"),
            "year": (pick("TDRC", "TYER", "DATE", "YEAR", "©DAY") or "")[:4], "isrc": pick("TSRC", "ISRC"),
            "bpm": pick("TBPM", "BPM", "TMPO"), "key": pick("TKEY", "INITIALKEY", "KEY"),
            "release_date": pick("TDRL", "RELEASEDATE"), "catalog": pick("CATALOGNUMBER", "TXXX"),
            "comments": pick("COMM", "COMMENT", "©CMT"), "mix_name": pick("TIT3", "SUBTITLE"),
        }
        tags = split_tags(fields["genre"], 1.0) + split_tags(pick("TIT1", "GROUPING", "©GRP"), 0.8)
        tags += split_tags(pick("TMOO", "MOOD"), 0.8) + split_tags(pick("STYLE"), 0.9)
        tags += [[h, 0.9] for h in re.findall(r"#([\w\-]+)", fields["comments"])]
        return self.result(state="found", fields=fields, tags=tags, match=1.0,
                           extra={"format": type(f).__name__, "length": round(getattr(f.info, "length", 0) or 0)})


@register
class Beets(Source):
    id, name, online, trust, needs = "beets", "beets", False, 0.8, ("library",)

    def lookup(self, q: TrackQuery) -> Result:
        db = Path(self.settings["library"]).expanduser()
        if not db.is_file():
            return self.result(state="error", note=f"No beets library at {db}")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("select * from items where path = ? or lower(path) = lower(?)",
                              (q.path.encode(), q.path.encode())).fetchone()
            if row is None:
                row = con.execute("select * from items where lower(artist)=lower(?) and lower(title)=lower(?)",
                                  (q.artist, q.title)).fetchone()
            if row is None:
                return self.result(state="none")
            flex = {r[0]: r[1] for r in con.execute("select key, value from item_attributes where entity_id=?",
                                                    (row["id"],))}
        finally:
            con.close()
        d = dict(row)
        fields = {"title": d.get("title"), "artist": d.get("artist"), "album": d.get("album"),
                  "album_artist": d.get("albumartist"), "label": d.get("label"), "genre": d.get("genre"),
                  "year": d.get("year") or None, "bpm": d.get("bpm") or None, "key": d.get("initial_key"),
                  "isrc": d.get("isrc"), "catalog": d.get("catalognum"), "comments": d.get("comments")}
        tags = split_tags(d.get("genre", ""), 1.0) + split_tags(flex.get("style", ""), 0.9)
        tags += split_tags(flex.get("mood", ""), 0.7) + split_tags(d.get("grouping", ""), 0.7)
        url = f"https://musicbrainz.org/recording/{d['mb_trackid']}" if d.get("mb_trackid") else ""
        return self.result(state="found", fields=fields, tags=tags, url=url, match=1.0)


@register
class Lexicon(Source):
    id, name, trust = "lexicon", "Lexicon", 1.0

    def lookup(self, q: TrackQuery) -> Result:
        base = (self.settings.get("url") or "http://localhost:48624").rstrip("/")
        try:
            data = self.get_json(f"{base}/v1/search/tracks",
                                 params={"filter[artist]": q.artist, "filter[title]": base_title(q.title)}, timeout=4)
        except Exception as e:
            return self.result(state="off", note=f"Lexicon isn't reachable (is it running with the Local API on?): {e}"[:160])
        tracks = _find_list(data, "title")
        best = max(tracks, key=lambda t: similarity(q, t.get("artist", ""), t.get("title", "")), default=None)
        if not best:
            return self.result(state="none")
        score = similarity(q, best.get("artist", ""), best.get("title", ""))
        fields = {"title": best.get("title"), "artist": best.get("artist"), "album": best.get("albumTitle"),
                  "label": best.get("label"), "genre": best.get("genre"), "year": best.get("year"),
                  "bpm": best.get("bpm"), "key": best.get("key"), "remixer": best.get("remixer"),
                  "mix_name": best.get("mixName"), "comments": best.get("comment")}
        tags = split_tags(best.get("genre", ""), 1.0) + split_tags(best.get("grouping", ""), 0.7)
        extra = {k: best.get(k) for k in ("energy", "danceability", "happiness", "popularity", "rating") if best.get(k)}
        return self.result(state="found", fields=fields, tags=tags, extra=extra, match=score)


@register
class MusicBrainz(Source):
    id, name, trust, min_interval = "musicbrainz", "MusicBrainz", 0.7, 1.1

    def lookup(self, q: TrackQuery) -> Result:
        api = "https://musicbrainz.org/ws/2"
        if q.isrc:
            data = self.get_json(f"{api}/isrc/{quote(q.isrc)}", params={"fmt": "json"})
            recs = data.get("recordings", [])
        else:
            query = f'recording:"{base_title(q.title)}" AND artist:"{q.artist}"'
            recs = self.get_json(f"{api}/recording", params={"query": query, "fmt": "json", "limit": 8}).get("recordings", [])
        scored = [(similarity(q, " ".join(a.get("name", "") for a in r.get("artist-credit", [])), r.get("title", "")), r)
                  for r in recs]
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < 0.6:
            return self.result(state="none")
        score, rec = scored[0]
        full = self.get_json(f"{api}/recording/{rec['id']}",
                             params={"inc": "artist-credits+isrcs+genres+tags+releases", "fmt": "json"})
        rel = (full.get("releases") or [{}])[0]
        label = ""
        if rel.get("id"):
            try:
                rd = self.get_json(f"{api}/release/{rel['id']}", params={"inc": "labels", "fmt": "json"})
                li = (rd.get("label-info") or [{}])[0]
                label = (li.get("label") or {}).get("name", "")
                catalog = li.get("catalog-number", "")
            except Exception:
                catalog = ""
        else:
            catalog = ""
        counts = [(g["name"], g.get("count", 1)) for g in full.get("genres", [])] + \
                 [(t["name"], t.get("count", 1) * 0.7) for t in full.get("tags", [])]
        top = max((c for _, c in counts), default=1) or 1
        fields = {"title": full.get("title"), "artist": "".join(a.get("name", "") + a.get("joinphrase", "")
                                                                for a in full.get("artist-credit", [])),
                  "album": rel.get("title"), "release_date": rel.get("date"), "year": (rel.get("date") or "")[:4],
                  "isrc": (full.get("isrcs") or [""])[0], "label": label, "catalog": catalog}
        return self.result(state="found", fields=fields, tags=[[n, round(c / top, 2)] for n, c in counts],
                           url=f"https://musicbrainz.org/recording/{rec['id']}", match=score)


@register
class LastFm(Source):
    id, name, trust, needs, min_interval = "lastfm", "Last.fm", 0.8, ("api_key",), 0.25

    def lookup(self, q: TrackQuery) -> Result:
        api = "https://ws.audioscrobbler.com/2.0/"
        base = {"api_key": self.settings["api_key"], "format": "json", "autocorrect": 1}
        info = self.get_json(api, params={**base, "method": "track.getInfo", "artist": q.artist,
                                          "track": base_title(q.title)})
        if "error" in info:
            return self.result(state="none", note=info.get("message", ""))
        t = info.get("track", {})
        top = self.get_json(api, params={**base, "method": "track.getTopTags", "artist": q.artist,
                                         "track": base_title(q.title)})
        tags = [[x["name"], int(x.get("count", 0)) / 100] for x in top.get("toptags", {}).get("tag", [])[:20]
                if int(x.get("count", 0)) >= 5]
        if not tags:
            at = self.get_json(api, params={**base, "method": "artist.getTopTags", "artist": q.artist})
            tags = [[x["name"], int(x.get("count", 0)) / 200] for x in at.get("toptags", {}).get("tag", [])[:12]
                    if int(x.get("count", 0)) >= 10]
        fields = {"title": t.get("name"), "artist": (t.get("artist") or {}).get("name"),
                  "album": (t.get("album") or {}).get("title")}
        return self.result(state="found", fields=fields, tags=tags, url=t.get("url", ""),
                           match=similarity(q, fields["artist"] or "", fields["title"] or ""),
                           extra={"listeners": t.get("listeners"), "playcount": t.get("playcount")})


@register
class Discogs(Source):
    id, name, trust, needs, min_interval = "discogs", "Discogs", 0.9, ("token",), 1.1

    def lookup(self, q: TrackQuery) -> Result:
        data = self.get_json("https://api.discogs.com/database/search",
                             params={"type": "release", "artist": q.artist, "track": base_title(q.title),
                                     "per_page": 8, "token": self.settings["token"]})
        res = data.get("results", [])
        if not res:
            return self.result(state="none")
        r = res[0]
        rel_artist, _, rel_title = (r.get("title") or "").partition(" - ")
        fields = {"album": rel_title, "label": (r.get("label") or [""])[0], "year": r.get("year"),
                  "genre": ", ".join(r.get("genre") or []), "catalog": r.get("catno")}
        tags = [[s, 1.0] for s in r.get("style") or []] + [[g, 0.6] for g in r.get("genre") or []]
        url = f"https://www.discogs.com{r['uri']}" if r.get("uri") else ""
        return self.result(state="found", fields=fields, tags=tags, url=url,
                           extra={"format": ", ".join(r.get("format") or []), "country": r.get("country")},
                           match=0.75 if similarity(q, rel_artist, q.title) > 0.5 else 0.5)


@register
class SoundCloud(Source):
    id, name, trust, needs, min_interval = "soundcloud", "SoundCloud", 0.5, ("client_id", "client_secret"), 0.5
    _token: str = ""

    def _auth(self) -> str:
        if not SoundCloud._token:
            r = self.http("POST", "https://secure.soundcloud.com/oauth/token",
                          data={"grant_type": "client_credentials"},
                          auth=(self.settings["client_id"], self.settings["client_secret"]))
            SoundCloud._token = r.json()["access_token"]
        return SoundCloud._token

    def lookup(self, q: TrackQuery) -> Result:
        hdr = {"Authorization": f"OAuth {self._auth()}", "accept": "application/json"}
        res = self.get_json("https://api.soundcloud.com/tracks", headers=hdr,
                            params={"q": f"{q.artist} {base_title(q.title)}", "limit": 8})
        res = res.get("collection", res) if isinstance(res, dict) else res
        if not res:
            return self.result(state="none")
        best = max(res, key=lambda t: similarity(q, (t.get("user") or {}).get("username", ""), t.get("title", "")))
        try:
            tl = shlex.split(best.get("tag_list") or "")
        except ValueError:
            tl = (best.get("tag_list") or "").split()
        fields = {"title": best.get("title"), "artist": (best.get("user") or {}).get("username"),
                  "genre": best.get("genre"), "label": best.get("label_name"), "year": best.get("release_year"),
                  "bpm": best.get("bpm"), "isrc": best.get("isrc")}
        tags = split_tags(best.get("genre") or "", 1.0) + [[t, 0.6] for t in tl[:15]]
        return self.result(state="found", fields=fields, tags=tags, url=best.get("permalink_url", ""),
                           match=similarity(q, fields["artist"] or "", fields["title"] or ""),
                           extra={"plays": best.get("playback_count"), "likes": best.get("likes_count")})


@register
class Beatport(Source):
    """Reads the JSON embedded in Beatport's public search page (the way One Tagger does).
    Not an official API: it can stop working when Beatport changes their site. Off by default."""
    id, name, trust, default_on, min_interval = "beatport", "Beatport", 1.0, False, 1.5

    def lookup(self, q: TrackQuery) -> Result:
        html = self.http("GET", "https://www.beatport.com/search/tracks",
                         params={"q": f"{q.artist} {base_title(q.title)}"}).text
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return self.result(state="error", note="Beatport's page layout changed")
        tracks = _find_list(json.loads(m.group(1)), "track_name")
        if not tracks:
            return self.result(state="none")

        def artists(t):
            return ", ".join(a.get("artist_name", "") for a in t.get("artists") or [])

        best = max(tracks, key=lambda t: similarity(q, artists(t), t.get("track_name", "")))
        score = similarity(q, artists(best), best.get("track_name", ""))
        if score < 0.6:
            return self.result(state="none")
        g = best.get("genre") or [{}]
        g = g[0] if isinstance(g, list) else g
        sg = best.get("sub_genre") or {}
        sg = sg[0] if isinstance(sg, list) and sg else sg
        fields = {"title": best.get("track_name"), "mix_name": best.get("mix_name"), "artist": artists(best),
                  "remixer": ", ".join(a.get("artist_name", "") for a in best.get("remixers") or []),
                  "label": (best.get("label") or {}).get("label_name"), "genre": g.get("genre_name"),
                  "bpm": best.get("bpm"), "key": best.get("key_name"), "release_date": best.get("release_date"),
                  "year": (best.get("release_date") or "")[:4], "isrc": best.get("isrc"),
                  "catalog": best.get("catalog_number")}
        tags = [[g.get("genre_name", ""), 1.0]] + ([[sg.get("sub_genre_name", ""), 1.0]] if sg else [])
        url = f"https://www.beatport.com/track/x/{best['track_id']}" if best.get("track_id") else ""
        return self.result(state="found", fields=fields, tags=[t for t in tags if t[0]], url=url, match=score)


def _find_list(obj, key: str) -> list[dict]:
    """First list of dicts anywhere in a JSON blob whose items have `key`."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, list):
            if o and isinstance(o[0], dict) and key in o[0]:
                return o
            stack.extend(o)
        elif isinstance(o, dict):
            stack.extend(o.values())
    return []
