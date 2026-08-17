# musicseerr — a Jellyseerr-style request app for music.
#
# Search on Deezer, click download: the track is fetched from YouTube,
# tagged with proper metadata from Deezer (including album_artist), and
# placed into a Navidrome/Jellyfin-friendly folder layout:
#     Music/<album artist>/<album>/<artist> - <title>.mp3
# Navidrome's file watcher picks new files up on its own — no rescan needed.
#
# Design notes:
#   - Tags come from Deezer (data, not guesses). Navidrome derives the
#     artist from the album_artist TAG, not from the folder — leaving it
#     empty files the track under the wrong artist.
#   - album_artist = the ALBUM's artist (fetched from Deezer's /album
#     endpoint). On compilations this differs from the track artist.
#   - ID3v2.3 is written for maximum player compatibility, and the source
#     YouTube URL is stored in TXXX:purl.
#   - Downloads run on a SINGLE worker, sequentially. This is designed for
#     small servers (Raspberry Pi class): parallel downloads + ffmpeg can
#     easily starve the box, and YouTube dislikes bursts anyway.
#   - A transient YouTube 403 happens now and then; every job gets one
#     automatic retry, after that a manual retry button in the UI.
#
# Legal: intended for personal use. Downloading from YouTube may violate
# YouTube's Terms of Service; there is no uploading/seeding involved.
import os
import random
import re
import shutil
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "mp3")
AUDIO_QUALITY = os.environ.get("AUDIO_QUALITY", "0")  # yt-dlp scale: 0 = best
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")  # optional, for bot checks
# optional: a bgutil-ytdlp-pot-provider instance. As of mid-2026 YouTube
# demands PO tokens for real formats ("Sign in to confirm you're not a
# bot") — without a provider most downloads fail.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "")
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "queue.db"
STATIC = Path(__file__).parent / "static"
DEEZER = "https://api.deezer.com"

app = FastAPI(title="musicseerr")


# ── database ────────────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # migrate a v1 (Turkish-named) database if one is present
    old = DATA_DIR / "kuyruk.db"
    if old.exists() and not DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(old) + suffix)
            if src.exists():
                os.replace(src, Path(str(DB_PATH) + suffix))
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deezer_id INTEGER NOT NULL,
            title TEXT, artist TEXT, album TEXT, album_artist TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            error TEXT, file TEXT, youtube TEXT,
            added TEXT, finished TEXT, attempts INTEGER DEFAULT 0)""")
        con.execute("PRAGMA journal_mode=WAL")
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "isler" in tables:  # v1 schema -> move rows over, keep history
            con.execute("""INSERT INTO jobs (id, deezer_id, title, artist, album,
                             album_artist, status, error, file, youtube, added,
                             finished, attempts)
                SELECT id, deezer_id, baslik, sanatci, album, album_sanatcisi,
                    CASE durum WHEN 'bekliyor' THEN 'queued'
                               WHEN 'iniyor' THEN 'downloading'
                               WHEN 'tamam' THEN 'done'
                               WHEN 'zaten-var' THEN 'exists'
                               WHEN 'hata' THEN 'error' ELSE durum END,
                    hata, dosya, youtube, eklendi, bitti, COALESCE(deneme, 0)
                FROM isler""")
            con.execute("DROP TABLE isler")


# ── helpers ─────────────────────────────────────────────────────────────────
def clean_dir_name(s: str) -> str:
    # '/' and ':' are invalid in folder names; leading dots hide the folder,
    # trailing dots/spaces break some filesystems
    s = (s or "").replace("/", "-").replace(":", "-")
    return re.sub(r"^[ .]+|[ .]+$", "", s)


def clean_file_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "-", s or "")
    return re.sub(r"^[ .]+|[ .]+$", "", s)


def target_path(album_artist: str, album: str, artist: str, title: str) -> Path:
    aa = clean_dir_name(album_artist) or "Unknown Artist"
    al = clean_dir_name(album) or "Unknown Album"
    name = clean_file_name(f"{artist} - {title}") + f".{AUDIO_FORMAT}"
    return MUSIC_DIR / aa / al / name


def deezer(path: str, **params):
    r = requests.get(f"{DEEZER}/{path}", params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(f"Deezer error: {d['error']}")
    return d


def track_summary(t: dict) -> dict:
    alb = t.get("album") or {}
    out = {
        "deezer_id": t["id"],
        "title": t.get("title", "?"),
        "artist": (t.get("artist") or {}).get("name", "?"),
        "album": alb.get("title", ""),
        "duration": t.get("duration", 0),
        "cover": alb.get("cover_medium") or "",
    }
    # approximate existence check (uses track artist; the worker resolves the
    # real album artist at download time)
    out["exists"] = target_path(out["artist"], out["album"], out["artist"], out["title"]).exists()
    return out


def album_summary(a: dict) -> dict:
    return {
        "album_id": a["id"],
        "title": a.get("title", "?"),
        "artist": (a.get("artist") or {}).get("name", "?"),
        "cover": a.get("cover_medium", ""),
        "track_count": a.get("nb_tracks", 0),
        "record_type": a.get("record_type", ""),
        "year": (a.get("release_date") or "")[:4],
    }


# ── API: pages & search ─────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "queue_db": DB_PATH.exists(),
            "music_writable": os.access(MUSIC_DIR, os.W_OK)}


@app.get("/api/search")
def search(q: str, limit: int = 24):
    d = deezer("search", q=q, limit=min(limit, 40))
    return {"results": [track_summary(t) for t in d.get("data", [])]}


@app.get("/api/popular")
def popular(limit: int = 15):
    d = deezer("chart/0/tracks", limit=min(limit, 30))
    return {"results": [track_summary(t) for t in d.get("data", [])]}


@app.get("/api/search-album")
def search_album(q: str, limit: int = 18):
    d = deezer("search/album", q=q, limit=min(limit, 30))
    return {"results": [album_summary(a) for a in d.get("data", [])]}


@app.get("/api/search-artist")
def search_artist(q: str, limit: int = 12):
    d = deezer("search/artist", q=q, limit=min(limit, 20))
    return {"results": [{"artist_id": a["id"], "name": a.get("name", "?"),
                         "picture": a.get("picture_medium", "")}
                        for a in d.get("data", [])]}


@app.get("/api/artist/{artist_id}")
def artist_page(artist_id: int):
    a = deezer(f"artist/{artist_id}")
    top = deezer(f"artist/{artist_id}/top", limit=25)
    albums = deezer(f"artist/{artist_id}/albums", limit=100)
    return {
        "artist_id": a["id"], "name": a.get("name", "?"),
        "picture": a.get("picture_medium", ""),
        "top": [track_summary(t) for t in top.get("data", [])],
        "albums": [album_summary(x) for x in albums.get("data", [])],
    }


@app.get("/api/cover")
def cover(u: str):
    # Proxy covers so the browser never talks to Deezer's CDN directly
    # (some setups block third-party images). Only dzcdn.net is allowed —
    # this must not become an open proxy.
    from urllib.parse import urlparse
    host = urlparse(u).hostname or ""
    if not host.endswith(".dzcdn.net"):
        raise HTTPException(400, "only Deezer covers are proxied")
    r = requests.get(u, timeout=15)
    return Response(r.content, media_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=604800"})


# ── API: queueing ───────────────────────────────────────────────────────────
def enqueue_tracks(tracks: list, album: str, album_artist: str) -> dict:
    """Queue a list of Deezer track dicts; skip files that already exist
    and tracks already waiting in the queue."""
    added, skipped = 0, 0
    with db() as con:
        pending = {r["deezer_id"] for r in con.execute(
            "SELECT deezer_id FROM jobs WHERE status IN ('queued','downloading')")}
        for t in tracks:
            artist = (t.get("artist") or {}).get("name", "?")
            alb = album or (t.get("album") or {}).get("title", "")
            aa = album_artist or artist
            if t["id"] in pending or target_path(aa, alb, artist, t.get("title", "")).exists():
                skipped += 1
                continue
            con.execute(
                "INSERT INTO jobs (deezer_id, title, artist, album, album_artist, added)"
                " VALUES (?,?,?,?,?,?)",
                (t["id"], t.get("title", "?"), artist, alb, aa,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            pending.add(t["id"])
            added += 1
    return {"added": added, "skipped": skipped}


class DownloadRequest(BaseModel):
    deezer_id: int


@app.post("/api/download")
def download(req: DownloadRequest):
    with db() as con:
        open_job = con.execute(
            "SELECT id, status FROM jobs WHERE deezer_id=? AND status IN ('queued','downloading')",
            (req.deezer_id,)).fetchone()
        if open_job:
            return {"job_id": open_job["id"], "status": open_job["status"],
                    "note": "already queued"}
        # metadata is resolved server-side; the client only sends an id
        t = deezer(f"track/{req.deezer_id}")
        alb = deezer(f"album/{t['album']['id']}")
        cur = con.execute(
            "INSERT INTO jobs (deezer_id, title, artist, album, album_artist, added)"
            " VALUES (?,?,?,?,?,?)",
            (t["id"], t["title"], t["artist"]["name"], alb["title"],
             (alb.get("artist") or {}).get("name") or t["artist"]["name"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return {"job_id": cur.lastrowid, "status": "queued"}


def album_tracks(album_id: int) -> list:
    # tracks.data may be truncated -> page through the /tracks endpoint
    tracks, page = [], 0
    while page < 4:
        d = deezer(f"album/{album_id}/tracks", limit=100, index=page * 100)
        tracks += d.get("data", [])
        if not d.get("next"):
            break
        page += 1
    return tracks


class AlbumRequest(BaseModel):
    album_id: int


@app.post("/api/download-album")
def download_album(req: AlbumRequest):
    alb = deezer(f"album/{req.album_id}")
    aa = (alb.get("artist") or {}).get("name", "")
    result = enqueue_tracks(album_tracks(req.album_id), alb.get("title", ""), aa)
    result["album"] = alb.get("title", "")
    return result


class ArtistRequest(BaseModel):
    artist_id: int
    mode: str = "top"  # "top" (top 25) | "albums" (full discography)


@app.post("/api/download-artist")
def download_artist(req: ArtistRequest):
    if req.mode == "albums":
        albums = deezer(f"artist/{req.artist_id}/albums", limit=100).get("data", [])
        total = {"added": 0, "skipped": 0, "album_count": 0}
        for a in albums:
            # proper albums + EPs; singles usually duplicate album tracks
            if a.get("record_type") not in ("album", "ep"):
                continue
            r = enqueue_tracks(album_tracks(a["id"]), a.get("title", ""),
                               (a.get("artist") or {}).get("name", ""))
            total["added"] += r["added"]
            total["skipped"] += r["skipped"]
            total["album_count"] += 1
        return total
    top = deezer(f"artist/{req.artist_id}/top", limit=25).get("data", [])
    return enqueue_tracks(top, "", "")


class ListRequest(BaseModel):
    tracks: list  # [{deezer_id, title, artist, album}]


@app.post("/api/download-list")
def download_list(req: ListRequest):
    tracks = [{"id": t["deezer_id"], "title": t.get("title", "?"),
               "artist": {"name": t.get("artist", "?")},
               "album": {"title": t.get("album", "")}}
              for t in req.tracks[:400] if t.get("deezer_id")]
    return enqueue_tracks(tracks, "", "")


# ── API: CSV import ─────────────────────────────────────────────────────────
# Accepts Exportify (Spotify), iTunes exports, Google Takeout's
# music-library-songs.csv, or a plain headerless "artist,title[,album]" list.
# Every row is matched against Deezer; NOTHING downloads until the user
# confirms the preview and calls /api/download-list.
CSV_COLUMNS = {
    "title": ["track name", "track", "title", "song title", "song", "name"],
    "artist": ["artist name(s)", "artist names", "artist name", "artists", "artist"],
    "album": ["album name", "album title", "album_name", "album"],
}


def csv_find_columns(headers: list) -> dict:
    idx = {}
    for j, h in enumerate(headers):
        h2 = (h or "").strip().lower()
        for field, candidates in CSV_COLUMNS.items():
            if field in idx:
                continue
            # an "album artist" column must match neither artist nor album
            if field == "artist" and "album" in h2:
                continue
            if field == "album" and "artist" in h2:
                continue
            if any(h2 == c or (c in h2 and len(c) > 4) for c in candidates):
                idx[field] = j
                break
    return idx


class CsvRequest(BaseModel):
    csv: str


@app.post("/api/csv-match")
def csv_match(req: CsvRequest):
    import csv as csvmod
    import io
    content = req.csv.lstrip("﻿")
    try:
        dialect = csvmod.Sniffer().sniff(content[:2000], delimiters=",;\t")
    except csvmod.Error:
        dialect = csvmod.excel
    rows = [r for r in csvmod.reader(io.StringIO(content), dialect)
            if any((c or "").strip() for c in r)]
    if not rows:
        raise HTTPException(400, "the CSV looks empty")
    if len(rows) > 401:
        raise HTTPException(400, "max 400 rows per file (Deezer search quota) — split the file")

    idx = csv_find_columns(rows[0])
    if "title" in idx and "artist" in idx:
        data = rows[1:]
    else:
        # no header row: assume artist, title[, album]
        idx = {"artist": 0, "title": 1, "album": 2}
        data = rows

    def cell(row, field):
        j = idx.get(field)
        return row[j].strip() if j is not None and j < len(row) else ""

    out, matched = [], 0
    for i, row in enumerate(data):
        artist = cell(row, "artist").split(",")[0].strip()
        title = cell(row, "title")
        rec = {"row": i + 1, "csv_artist": artist or cell(row, "artist"),
               "csv_title": title, "match": None}
        if artist and title:
            try:
                d = deezer("search", q=f'artist:"{artist}" track:"{title}"', limit=1)
                data_ = d.get("data") or deezer(
                    "search", q=f"{artist} {title}", limit=1).get("data")
                if data_:
                    rec["match"] = track_summary(data_[0])
                    matched += 1
            except Exception:
                pass  # one bad row must not sink the whole file
            time.sleep(0.2)  # Deezer quota: 50 requests / 5 s — stay under
        out.append(rec)
    return {"total": len(out), "matched": matched, "rows": out}


# ── API: queue state ────────────────────────────────────────────────────────
@app.post("/api/retry/{job_id}")
def retry(job_id: int):
    with db() as con:
        r = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such job")
        if r["status"] != "error":
            return {"job_id": job_id, "status": r["status"], "note": "not in error state"}
        con.execute("UPDATE jobs SET status='queued', error=NULL, attempts=0,"
                    " finished=NULL WHERE id=?", (job_id,))
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/job/{job_id}")
def job_status(job_id: int):
    with db() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no such job")
    return dict(r)


@app.get("/api/queue")
def queue():
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 30")]
        pending = con.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','downloading')"
        ).fetchone()["c"]
    return {"jobs": rows, "pending": pending}


# ── worker: single-lane downloads ───────────────────────────────────────────
def _ydl_base_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 30}
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}}
    return opts


def _pick_youtube(artist: str, title: str, duration: int):
    """Search YouTube; prefer duration proximity and official 'Topic'
    (auto-generated audio) channels over remixes and live versions."""
    from yt_dlp import YoutubeDL
    with YoutubeDL({**_ydl_base_opts(), "extract_flat": True}) as y:
        found = y.extract_info(f"ytsearch8:{artist} - {title}", download=False)
    candidates = []
    for e in (found or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        diff = abs((e.get("duration") or 0) - duration) if duration else 0
        # >35 s off is probably a remix or live take — penalize hard
        score = diff + (0 if str(e.get("channel", "")).endswith(" - Topic") else 40) \
                + (200 if duration and diff > 35 else 0)
        candidates.append((score, e))
    if not candidates:
        raise RuntimeError("no results found on YouTube")
    candidates.sort(key=lambda x: x[0])
    e = candidates[0][1]
    return f"https://www.youtube.com/watch?v={e['id']}", e.get("title", "")


def _download_and_tag(job: dict):
    from yt_dlp import YoutubeDL
    from mutagen.id3 import (APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS,
                             TRCK, TXXX)
    from mutagen.mp3 import MP3

    t = deezer(f"track/{job['deezer_id']}")
    alb = deezer(f"album/{t['album']['id']}")
    artist = t["artist"]["name"]
    # 'contributors' lists featured artists too: all of them go into TPE1,
    # the album artist alone into TPE2
    all_artists = ", ".join(dict.fromkeys(
        [c["name"] for c in t.get("contributors") or []] or [artist]))
    album_artist = (alb.get("artist") or {}).get("name") or artist
    title = t["title"]

    target = target_path(album_artist, alb["title"], artist, title)
    if target.exists():
        return "exists", str(target.relative_to(MUSIC_DIR)), ""

    yt_url, _yt_title = _pick_youtube(artist, title, t.get("duration") or 0)

    tmp = TMP_DIR / f"job-{job['id']}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with YoutubeDL({
            **_ydl_base_opts(),
            "format": "bestaudio/best",
            "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": AUDIO_FORMAT,
                                "preferredquality": AUDIO_QUALITY}],
            "noplaylist": True, "retries": 5,
        }) as y:
            y.extract_info(yt_url, download=True)
        files = list(tmp.glob(f"*.{AUDIO_FORMAT}"))
        if not files:
            raise RuntimeError(f"download produced no .{AUDIO_FORMAT} file")
        f = files[0]

        # ── tags: from Deezer, ID3v2.3 ──
        audio = MP3(f)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags.delall("TIT2"); tags.add(TIT2(encoding=3, text=title))
        tags.delall("TPE1"); tags.add(TPE1(encoding=3, text=all_artists))
        tags.delall("TPE2"); tags.add(TPE2(encoding=3, text=album_artist))
        tags.delall("TALB"); tags.add(TALB(encoding=3, text=alb["title"]))
        if t.get("track_position"):
            tags.delall("TRCK"); tags.add(TRCK(encoding=3, text=str(t["track_position"])))
        if t.get("disk_number"):
            tags.delall("TPOS"); tags.add(TPOS(encoding=3, text=str(t["disk_number"])))
        year = (alb.get("release_date") or "")[:4]
        if year:
            tags.delall("TDRC"); tags.add(TDRC(encoding=3, text=year))
        genres = [g["name"] for g in (alb.get("genres") or {}).get("data") or []]
        if genres:
            tags.delall("TCON"); tags.add(TCON(encoding=3, text=genres[0]))
        # source URL, e.g. for later playlist matching
        tags.add(TXXX(encoding=3, desc="purl", text=yt_url))
        cover_url = alb.get("cover_big") or alb.get("cover_medium")
        if cover_url:
            cr = requests.get(cover_url, timeout=20)
            if cr.ok:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                              desc="Cover", data=cr.content))
        audio.save(v2_version=3)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(target))
        return "done", str(target.relative_to(MUSIC_DIR)), yt_url
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def worker():
    while True:
        try:
            with db() as con:
                r = con.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
                ).fetchone()
            if not r:
                time.sleep(3)
                continue
            with db() as con:
                con.execute("UPDATE jobs SET status='downloading' WHERE id=?", (r["id"],))
            try:
                status, file, yt = _download_and_tag(dict(r))
                with db() as con:
                    con.execute(
                        "UPDATE jobs SET status=?, file=?, youtube=?, finished=? WHERE id=?",
                        (status, file, yt,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            except Exception as e:
                traceback.print_exc()
                with db() as con:
                    attempts = (con.execute(
                        "SELECT attempts FROM jobs WHERE id=?",
                        (r["id"],)).fetchone()["attempts"] or 0)
                    if attempts < 1:
                        # transient YouTube 403s usually pass on a fresh
                        # extraction — one automatic retry, then it's manual
                        con.execute("UPDATE jobs SET status='queued', attempts=? WHERE id=?",
                                    (attempts + 1, r["id"]))
                        time.sleep(20)
                    else:
                        con.execute(
                            "UPDATE jobs SET status='error', error=?, finished=? WHERE id=?",
                            (str(e)[:500],
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            # be gentle with YouTube — a slightly random gap looks less
            # robotic than a fixed one on long bulk queues
            time.sleep(2 + random.random() * 3)
        except Exception:
            traceback.print_exc()
            time.sleep(10)


@app.on_event("startup")
def startup():
    init_db()
    # put jobs interrupted by a restart back into the queue
    with db() as con:
        con.execute("UPDATE jobs SET status='queued' WHERE status='downloading'")
    threading.Thread(target=worker, daemon=True).start()
