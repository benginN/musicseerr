# HOMELAB — Müzik Kutusu (10 Ağu 2026)
#
# NE İŞE YARAR: Jellyseerr'in müzik karşılığı — Deezer'da ara, beğendiğin
# şarkıyı seç, uygulama YouTube'dan indirir, etiketler ve Navidrome'un
# klasör düzenine (Muzik/<album_artist>/<albüm>/) yerleştirir. Navidrome'un
# dosya izleyicisi ~1 dk içinde kendisi görür — tarama tetiklenmez.
#
# NEDEN BÖYLE:
#   - Etiketler Deezer'dan gelir (tahmin değil, veri): 8 Ağu 2026 dersi —
#     Navidrome sanatçıyı KLASÖRDEN değil album_artist ETİKETİNDEN üretir;
#     boş kalırsa şarkı yanlış sanatçının altına düşer.
#   - album_artist = ALBÜMÜN sanatçısı (Deezer /album ucundan). Derleme
#     albümlerde şarkı sanatçısından farklıdır; etiket-duzelt.sh'ın "ilk
#     isim" tahmininden daha doğru.
#   - id3v2.3 yazılır (etiket-duzelt.sh ile aynı) ve TXXX:purl'e YouTube
#     adresi konur (defterdeki playlist eşleştirme reçetesi purl okur).
#   - İndirme kuyruğu TEK işçi ile sıralı çalışır: Pi 4'te eşzamanlılık
#     yük olayı yaşattı (9 Ağu, yük 31) — burada baştan tek şerit.
#
# HUKUKİ NOT: torrent DEĞİL — yükleme (seed) yok. YouTube ToS ihlali
# kullanıcıyla konuşuldu (10 Ağu 2026), kişisel kullanım.
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
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

MUZIK = Path(os.environ.get("MUZIK_DIZIN", "/muzik"))
VERI = Path(os.environ.get("VERI_DIZIN", "/veri"))
GECICI = VERI / "gecici"
DB_YOLU = VERI / "kuyruk.db"
STATIK = Path(__file__).parent / "static"
DEEZER = "https://api.deezer.com"

app = FastAPI(title="Müzik Kutusu")


# ── veritabanı ──────────────────────────────────────────────────────────────
def db():
    b = sqlite3.connect(DB_YOLU)
    b.row_factory = sqlite3.Row
    return b


def db_kur():
    VERI.mkdir(parents=True, exist_ok=True)
    GECICI.mkdir(parents=True, exist_ok=True)
    with db() as b:
        b.execute("""CREATE TABLE IF NOT EXISTS isler (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deezer_id INTEGER NOT NULL,
            baslik TEXT, sanatci TEXT, album TEXT, album_sanatcisi TEXT,
            durum TEXT NOT NULL DEFAULT 'bekliyor',
            hata TEXT, dosya TEXT, youtube TEXT,
            eklendi TEXT, bitti TEXT)""")
        b.execute("PRAGMA journal_mode=WAL")


# ── yardımcılar ─────────────────────────────────────────────────────────────
def klasor_temizle(s: str) -> str:
    # muzik-klasorle.sh'ın temizle() kuralının aynısı: '/' ve ':' klasör
    # adında kullanılamaz; baştaki nokta gizler, sondaki nokta/boşluk sorun.
    s = (s or "").replace("/", "-").replace(":", "-")
    return re.sub(r"^[ .]+|[ .]+$", "", s)


def dosya_temizle(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "-", s or "")
    return re.sub(r"^[ .]+|[ .]+$", "", s)


def hedef_yolu(album_sanatcisi: str, album: str, sanatci: str, baslik: str) -> Path:
    aa = klasor_temizle(album_sanatcisi) or "Bilinmeyen Sanatci"
    alb = klasor_temizle(album) or "Bilinmeyen Album"
    ad = dosya_temizle(f"{sanatci} - {baslik}") + ".mp3"
    return MUZIK / aa / alb / ad


def deezer_al(yol: str, **params):
    r = requests.get(f"{DEEZER}/{yol}", params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(f"Deezer hatası: {d['error']}")
    return d


def parca_ozetle(t: dict) -> dict:
    alb = t.get("album") or {}
    ozet = {
        "deezer_id": t["id"],
        "baslik": t.get("title", "?"),
        "sanatci": (t.get("artist") or {}).get("name", "?"),
        "album": alb.get("title", ""),
        "sure": t.get("duration", 0),
        "kapak": alb.get("cover_medium") or t.get("md5_image", ""),
    }
    # album_artist albüm ucundan gelir (indirme anında); listelemede yaklaşık
    # değer olarak şarkı sanatçısıyla "zaten var mı" kontrolü yapıyoruz.
    ozet["var_mi"] = hedef_yolu(ozet["sanatci"], ozet["album"], ozet["sanatci"], ozet["baslik"]).exists()
    return ozet


# ── API ─────────────────────────────────────────────────────────────────────
@app.get("/")
def ana_sayfa():
    return FileResponse(STATIK / "index.html")


@app.get("/api/saglik")
def saglik():
    return {"durum": "ok", "kuyruk_db": DB_YOLU.exists(), "muzik_yazilabilir": os.access(MUZIK, os.W_OK)}


@app.get("/api/ara")
def ara(q: str, limit: int = 24):
    d = deezer_al("search", q=q, limit=min(limit, 40))
    return {"sonuclar": [parca_ozetle(t) for t in d.get("data", [])]}


@app.get("/api/populer")
def populer(limit: int = 15):
    # Deezer resmi listesi — Glance sekmesinin açılış içeriği
    d = deezer_al("chart/0/tracks", limit=min(limit, 30))
    return {"sonuclar": [parca_ozetle(t) for t in d.get("data", [])]}


@app.get("/api/kapak")
def kapak(u: str):
    # Kapak köprüsü: tarayıcı Deezer CDN'iyle hiç konuşmasın (Keşfet'teki
    # /afis/ dersinin aynısı — üçüncü taraf görüntüler bazı tarayıcılarda
    # engelleniyor). Yalnız dzcdn.net'e izin var (SSRF kapısı olmasın).
    from urllib.parse import urlparse
    ana = urlparse(u).hostname or ""
    if not ana.endswith(".dzcdn.net"):
        raise HTTPException(400, "yalnız Deezer kapakları")
    r = requests.get(u, timeout=15)
    return Response(r.content, media_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=604800"})


# ── toplu indirme (10 Ağu 2026, kullanıcı isteği) ──────────────────────────
def album_ozetle(a: dict) -> dict:
    return {
        "album_id": a["id"],
        "baslik": a.get("title", "?"),
        "sanatci": (a.get("artist") or {}).get("name", "?"),
        "kapak": a.get("cover_medium", ""),
        "parca_sayisi": a.get("nb_tracks", 0),
        "tur": a.get("record_type", ""),
        "yil": (a.get("release_date") or "")[:4],
    }


def album_parcalari(album_id: int) -> list:
    # tracks.data 25'te kesilebilir -> /tracks ucundan sayfalayarak al
    parcalar, sayfa = [], 0
    while sayfa < 4:
        d = deezer_al(f"album/{album_id}/tracks", limit=100, index=sayfa * 100)
        parcalar += d.get("data", [])
        if not d.get("next"):
            break
        sayfa += 1
    return parcalar


def parcalari_kuyrukla(parcalar: list, album: str, album_sanatcisi: str) -> dict:
    """Parça listesini kuyruğa al; zaten var olanları ve kuyruktakileri atla."""
    eklendi, atlandi = 0, 0
    with db() as b:
        acik = {r["deezer_id"] for r in b.execute(
            "SELECT deezer_id FROM isler WHERE durum IN ('bekliyor','iniyor')")}
        for t in parcalar:
            sanatci = (t.get("artist") or {}).get("name", "?")
            alb_ad = album or (t.get("album") or {}).get("title", "")
            aa = album_sanatcisi or sanatci
            if t["id"] in acik or hedef_yolu(aa, alb_ad, sanatci, t.get("title", "")).exists():
                atlandi += 1
                continue
            b.execute(
                "INSERT INTO isler (deezer_id, baslik, sanatci, album, album_sanatcisi, eklendi) VALUES (?,?,?,?,?,?)",
                (t["id"], t.get("title", "?"), sanatci, alb_ad, aa,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            acik.add(t["id"])
            eklendi += 1
    return {"eklendi": eklendi, "atlandi": atlandi}


@app.get("/api/ara-album")
def ara_album(q: str, limit: int = 18):
    d = deezer_al("search/album", q=q, limit=min(limit, 30))
    return {"sonuclar": [album_ozetle(a) for a in d.get("data", [])]}


@app.get("/api/ara-sanatci")
def ara_sanatci(q: str, limit: int = 12):
    d = deezer_al("search/artist", q=q, limit=min(limit, 20))
    return {"sonuclar": [{"sanatci_id": a["id"], "ad": a.get("name", "?"),
                          "resim": a.get("picture_medium", "")}
                         for a in d.get("data", [])]}


@app.get("/api/sanatci/{sanatci_id}")
def sanatci_sayfasi(sanatci_id: int):
    a = deezer_al(f"artist/{sanatci_id}")
    top = deezer_al(f"artist/{sanatci_id}/top", limit=25)
    albumler = deezer_al(f"artist/{sanatci_id}/albums", limit=100)
    return {
        "sanatci_id": a["id"], "ad": a.get("name", "?"),
        "resim": a.get("picture_medium", ""),
        "top": [parca_ozetle(t) for t in top.get("data", [])],
        "albumler": [album_ozetle(x) for x in albumler.get("data", [])],
    }


class AlbumIstek(BaseModel):
    album_id: int


@app.post("/api/indir-album")
def indir_album(istek: AlbumIstek):
    alb = deezer_al(f"album/{istek.album_id}")
    aa = (alb.get("artist") or {}).get("name", "")
    sonuc = parcalari_kuyrukla(album_parcalari(istek.album_id), alb.get("title", ""), aa)
    sonuc["album"] = alb.get("title", "")
    return sonuc


class SanatciIstek(BaseModel):
    sanatci_id: int
    mod: str = "top"  # "top" (en popüler 25) | "albumler" (tüm albümler)


@app.post("/api/indir-sanatci")
def indir_sanatci(istek: SanatciIstek):
    if istek.mod == "albumler":
        albumler = deezer_al(f"artist/{istek.sanatci_id}/albums", limit=100).get("data", [])
        toplam = {"eklendi": 0, "atlandi": 0, "album_sayisi": 0}
        for a in albumler:
            # single/derleme değil, asıl albümler (kullanıcı "albümleri" dedi)
            if a.get("record_type") not in ("album", "ep"):
                continue
            s = parcalari_kuyrukla(album_parcalari(a["id"]), a.get("title", ""),
                                   (a.get("artist") or {}).get("name", ""))
            toplam["eklendi"] += s["eklendi"]
            toplam["atlandi"] += s["atlandi"]
            toplam["album_sayisi"] += 1
        return toplam
    top = deezer_al(f"artist/{istek.sanatci_id}/top", limit=25).get("data", [])
    return parcalari_kuyrukla(top, "", "")


class IndirIstek(BaseModel):
    deezer_id: int


@app.post("/api/indir")
def indir(istek: IndirIstek):
    with db() as b:
        acik = b.execute(
            "SELECT id, durum FROM isler WHERE deezer_id=? AND durum IN ('bekliyor','iniyor')",
            (istek.deezer_id,)).fetchone()
        if acik:
            return {"is_id": acik["id"], "durum": acik["durum"], "not": "zaten kuyrukta"}
        # Etiket verisi sunucuda, istemciden gelen değil (istemci yalnız kimlik yollar)
        t = deezer_al(f"track/{istek.deezer_id}")
        alb = deezer_al(f"album/{t['album']['id']}")
        im = b.execute(
            "INSERT INTO isler (deezer_id, baslik, sanatci, album, album_sanatcisi, eklendi) VALUES (?,?,?,?,?,?)",
            (t["id"], t["title"], t["artist"]["name"], alb["title"],
             (alb.get("artist") or {}).get("name") or t["artist"]["name"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return {"is_id": im.lastrowid, "durum": "bekliyor"}


@app.get("/api/is/{is_id}")
def is_durumu(is_id: int):
    with db() as b:
        r = b.execute("SELECT * FROM isler WHERE id=?", (is_id,)).fetchone()
    if not r:
        raise HTTPException(404, "iş yok")
    return dict(r)


@app.get("/api/kuyruk")
def kuyruk():
    with db() as b:
        satirlar = [dict(r) for r in b.execute(
            "SELECT * FROM isler ORDER BY id DESC LIMIT 30")]
        bekleyen = b.execute(
            "SELECT COUNT(*) c FROM isler WHERE durum IN ('bekliyor','iniyor')").fetchone()["c"]
    return {"isler": satirlar, "bekleyen": bekleyen}


# ── işçi: tek şerit indirme ────────────────────────────────────────────────
def _youtube_sec(sanatci: str, baslik: str, sure: int):
    """ytsearch ile aday bul; süre yakınlığı + resmi 'Topic' kanalı önceliğiyle seç."""
    from yt_dlp import YoutubeDL
    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                    "socket_timeout": 30}) as y:
        arama = y.extract_info(f"ytsearch8:{sanatci} - {baslik}", download=False)
    adaylar = []
    for e in (arama or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        fark = abs((e.get("duration") or 0) - sure) if sure else 0
        # Süre 35 sn'den fazla şaşıyorsa muhtemelen remix/canlı — cezalandır
        puan = fark + (0 if str(e.get("channel", "")).endswith(" - Topic") else 40) \
               + (200 if sure and fark > 35 else 0)
        adaylar.append((puan, e))
    if not adaylar:
        raise RuntimeError("YouTube'da sonuç bulunamadı")
    adaylar.sort(key=lambda x: x[0])
    e = adaylar[0][1]
    return f"https://www.youtube.com/watch?v={e['id']}", e.get("title", "")


def _indir_ve_etiketle(is_kaydi: dict):
    from yt_dlp import YoutubeDL
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TCON, TXXX, APIC
    from mutagen.mp3 import MP3

    t = deezer_al(f"track/{is_kaydi['deezer_id']}")
    alb = deezer_al(f"album/{t['album']['id']}")
    sanatci = t["artist"]["name"]
    # Deezer 'contributors' düetleri de verir; TPE1'e hepsi, TPE2'ye albüm sanatçısı
    tum_sanatcilar = ", ".join(dict.fromkeys(
        [c["name"] for c in t.get("contributors") or []] or [sanatci]))
    album_sanatcisi = (alb.get("artist") or {}).get("name") or sanatci
    baslik = t["title"]

    hedef = hedef_yolu(album_sanatcisi, alb["title"], sanatci, baslik)
    if hedef.exists():
        return "zaten-var", str(hedef.relative_to(MUZIK)), ""

    yt_url, _yt_baslik = _youtube_sec(sanatci, baslik, t.get("duration") or 0)

    tmp = GECICI / f"is-{is_kaydi['id']}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with YoutubeDL({
            "format": "bestaudio/best",
            "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "0"}],
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "retries": 5, "socket_timeout": 30,
        }) as y:
            y.extract_info(yt_url, download=True)
        mp3ler = list(tmp.glob("*.mp3"))
        if not mp3ler:
            raise RuntimeError("indirme mp3 üretmedi")
        dosya = mp3ler[0]

        # ── etiket: Deezer verisiyle, id3v2.3 ──
        ses = MP3(dosya)
        if ses.tags is None:
            ses.add_tags()
        et = ses.tags
        et.delall("TIT2"); et.add(TIT2(encoding=3, text=baslik))
        et.delall("TPE1"); et.add(TPE1(encoding=3, text=tum_sanatcilar))
        et.delall("TPE2"); et.add(TPE2(encoding=3, text=album_sanatcisi))
        et.delall("TALB"); et.add(TALB(encoding=3, text=alb["title"]))
        if t.get("track_position"):
            et.delall("TRCK"); et.add(TRCK(encoding=3, text=str(t["track_position"])))
        if t.get("disk_number"):
            et.delall("TPOS"); et.add(TPOS(encoding=3, text=str(t["disk_number"])))
        yil = (alb.get("release_date") or "")[:4]
        if yil:
            et.delall("TDRC"); et.add(TDRC(encoding=3, text=yil))
        turler = [g["name"] for g in (alb.get("genres") or {}).get("data") or []]
        if turler:
            et.delall("TCON"); et.add(TCON(encoding=3, text=turler[0]))
        # purl: defterdeki YouTube playlist eşleştirme reçetesi bu etiketi okur
        et.add(TXXX(encoding=3, desc="purl", text=yt_url))
        kapak_url = alb.get("cover_big") or alb.get("cover_medium")
        if kapak_url:
            kr = requests.get(kapak_url, timeout=20)
            if kr.ok:
                et.delall("APIC")
                et.add(APIC(encoding=3, mime="image/jpeg", type=3,
                            desc="Cover", data=kr.content))
        ses.save(v2_version=3)

        hedef.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dosya), str(hedef))
        return "tamam", str(hedef.relative_to(MUZIK)), yt_url
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def isci():
    while True:
        try:
            with db() as b:
                r = b.execute("SELECT * FROM isler WHERE durum='bekliyor' ORDER BY id LIMIT 1").fetchone()
            if not r:
                time.sleep(3)
                continue
            with db() as b:
                b.execute("UPDATE isler SET durum='iniyor' WHERE id=?", (r["id"],))
            try:
                durum, dosya, yt = _indir_ve_etiketle(dict(r))
                with db() as b:
                    b.execute("UPDATE isler SET durum=?, dosya=?, youtube=?, bitti=? WHERE id=?",
                              (durum, dosya, yt,
                               datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            except Exception as e:
                traceback.print_exc()
                with db() as b:
                    b.execute("UPDATE isler SET durum='hata', hata=?, bitti=? WHERE id=?",
                              (str(e)[:500], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
            # YouTube'a nazik davran — toplu kuyruklarda sabit aralık robotik
            # görünür, hafif rastgelelik bot korumasını daha az kışkırtır
            time.sleep(2 + random.random() * 3)
        except Exception:
            traceback.print_exc()
            time.sleep(10)


@app.on_event("startup")
def basla():
    db_kur()
    # Yarım kalan işleri (konteyner yeniden başladıysa) kuyruğa geri koy
    with db() as b:
        b.execute("UPDATE isler SET durum='bekliyor' WHERE durum='iniyor'")
    threading.Thread(target=isci, daemon=True).start()
