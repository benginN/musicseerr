# Müzik Kutusu

Jellyseerr'in müzik karşılığı — ev sunucusundaki (Raspberry Pi) kendi uygulamamız.

**Ne yapar:** Deezer'da şarkı ararsın, ⬇ tuşuna basarsın; uygulama şarkıyı
YouTube'dan indirir, Deezer'ın verisiyle etiketler (`album_artist` dahil) ve
`Muzik/<albüm sanatçısı>/<albüm>/` klasörüne koyar. Navidrome'un dosya
izleyicisi ~1 dakika içinde kendisi görür.

- Arayüz: `http://100.114.66.37:8086` (evde `http://raspberrypi.local:8086`)
- Panelin sağ altındaki **Müzik** sekmesi aynı API'yi kullanır.

## Mimari

| Parça | Yol |
|---|---|
| Kod (bu repo) | `<T7>/muzik-kutusu/app` — origin Gitea `obepozdemir/muzik-kutusu` |
| Kuyruk veritabanı | `<T7>/muzik-kutusu/veri/kuyruk.db` (gece `sqlite3 .backup` → `data/MuzikKutusu/db-yedek`) |
| Compose | `/opt/muzik-kutusu/docker-compose.yml` |
| Müzik hedefi | `data/Muzik` (Navidrome'un okuduğu klasörün kendisi, yazılabilir bağlı) |

Tek işçi, sıralı indirme (Pi 4'te eşzamanlılık yük olayı yaşattı — 9 Ağu 2026).
Etiketler id3v2.3, `TXXX:purl`'e YouTube adresi yazılır (playlist eşleştirme
reçetesi bunu okur).

## İndirme bozulursa

Neredeyse her zaman yt-dlp'nin eskimesidir:

```
cd /opt/muzik-kutusu && sudo docker compose build --no-cache && sudo docker compose up -d
```
