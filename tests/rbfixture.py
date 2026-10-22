# tests/rbfixture.py : a small, unencrypted master.db built with pyrekordbox's own models.
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def make_master_db(folder: Path) -> Path:
    from pyrekordbox.masterdb import models as m
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "master.db"
    eng = create_engine(f"sqlite:///{db}")
    for table in m.Base.metadata.tables.values():   # real master.db has defaults we don't need here
        for col in table.columns:
            if not col.primary_key:
                col.nullable = True
    m.Base.metadata.create_all(eng)
    now = datetime(2026, 1, 1)
    stamp = dict(created_at=now, updated_at=now)
    with Session(eng) as s:
        s.add(m.AgentRegistry(registry_id="localUpdateCount", int_1=100, date_1=now, date_2=now, **stamp))
        s.add_all([m.DjmdArtist(ID="1", Name="Bianca Oblivion", **stamp), m.DjmdArtist(ID="2", Name="Ohm Seven", **stamp),
                   m.DjmdGenre(ID="1", Name="UK Bass", **stamp), m.DjmdLabel(ID="1", Name="Sub Zero Rec", **stamp),
                   m.DjmdAlbum(ID="1", Name="Sub Zero", **stamp),
                   m.DjmdKey(ID="1", ScaleName="Dm", Seq=1, **stamp), m.DjmdKey(ID="2", ScaleName="Am", Seq=2, **stamp)])
        s.add_all([
            m.DjmdContent(ID="101", FolderPath="C:/Music/Selecta.mp3", Title="Selecta", ArtistID="1", AlbumID="1",
                          GenreID="1", LabelID="1", KeyID="1", BPM=14000, Length=320, Commnt="uk bass",
                          ReleaseYear=2022, TrackNo=2, DiscNo=1, Rating=0, DJPlayCount="0", **stamp),
            m.DjmdContent(ID="102", FolderPath="C:/Music/Acid Fountain.mp3", Title="Acid Fountain", ArtistID="2",
                          KeyID="2", BPM=13200, Length=400, Commnt="", **stamp),
            m.DjmdContent(ID="103", FolderPath="C:/Music/Third.mp3", Title="Third", ArtistID="2", BPM=12400,
                          Length=380, Commnt="#warmup", **stamp),
        ])
        s.add_all([m.DjmdMyTag(ID="1", Name="Genre", Attribute=1, ParentID="root", Seq=1, **stamp),
                   m.DjmdMyTag(ID="2", Name="Dubstep", Attribute=0, ParentID="1", Seq=1, **stamp),
                   m.DjmdSongMyTag(ID="1", MyTagID="2", ContentID="101", TrackNo=1, **stamp)])
        s.add_all([m.DjmdPlaylist(ID="10", Name="Gigs", Attribute=1, ParentID="root", Seq=1, **stamp),
                   m.DjmdPlaylist(ID="11", Name="Friday", Attribute=0, ParentID="10", Seq=1, **stamp),
                   m.DjmdSongPlaylist(ID="1", PlaylistID="11", ContentID="101", TrackNo=1, **stamp),
                   m.DjmdSongPlaylist(ID="2", PlaylistID="11", ContentID="102", TrackNo=2, **stamp)])
        s.commit()
    return db


def opener_for(db: Path):
    def open_(path=None, key=""):
        from pyrekordbox import MasterDatabase
        return MasterDatabase(path=str(path or db), unlock=False)
    return open_
