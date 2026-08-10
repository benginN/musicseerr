# musicseerr
# NOTE: if downloads suddenly start failing, the usual suspect is an outdated
# yt-dlp (YouTube changes often). Rebuild to pull the latest:
#   docker compose build --no-cache && docker compose up -d
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" yt-dlp mutagen requests

WORKDIR /app
COPY app/ /app/

# run unprivileged; override with `user:` in docker-compose.yml to match
# the owner of your music folder
USER 1000:100
EXPOSE 8086
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8086"]
