# HOMELAB — Müzik Kutusu imajı (10 Ağu 2026)
# ⚠️ İNDİRME BOZULURSA İLK ŞÜPHELİ yt-dlp'nin ESKİMESİ — YouTube sık değişir.
# Çare: imajı yeniden kur (pip en yeni yt-dlp'yi alır):
#   cd /opt/muzik-kutusu && sudo docker compose build --no-cache muzik-kutusu && sudo docker compose up -d
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" yt-dlp mutagen requests

WORKDIR /app
COPY app/ /app/

# data/Muzik dosyaları obepozdemir:users (1000:100) sahipliğinde kalsın —
# SMB ve gecelik yedekle aynı sahiplik düzeni.
USER 1000:100
EXPOSE 8086
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8086"]
