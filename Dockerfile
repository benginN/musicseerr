# musicseerr
# NOTE: if downloads suddenly start failing, the usual suspect is an outdated
# yt-dlp (YouTube changes often). Rebuild to pull the latest:
#   docker compose build --no-cache && docker compose up -d
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# deno: yt-dlp needs a JavaScript runtime for YouTube extraction (EJS) —
# without one YouTube serves few/no formats. deno is yt-dlp's default runtime.
RUN case "$(dpkg --print-architecture)" in \
      arm64) DENO_ARCH=aarch64-unknown-linux-gnu ;; \
      *)     DENO_ARCH=x86_64-unknown-linux-gnu ;; \
    esac \
 && curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip \
 && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip \
 && deno --version

# bgutil-ytdlp-pot-provider: yt-dlp plugin that fetches YouTube PO tokens
# from a companion bgutil provider server (set POT_PROVIDER_URL).
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" yt-dlp mutagen requests \
    bgutil-ytdlp-pot-provider

WORKDIR /app
COPY app/ /app/

# run unprivileged; override with `user:` in docker-compose.yml to match
# the owner of your music folder
USER 1000:100
EXPOSE 8086
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8086"]
