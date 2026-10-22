# Multi-arch friendly (amd64 and arm64, e.g. Apple Silicon or ARM servers):
#   docker build -t grokbot-publisher .
#   docker run --rm --env-file .env --user "$(id -u):$(id -g)" \
#     -v "$PWD/grokbot-cabinet:/data/cabinet" -v "$HOME/.config/grokbot-publisher:/data/config" \
#     grokbot-publisher watch
# (--user keeps files written to the bind mounts owned by you, not uid 10001.)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 publisher \
 && mkdir -p /data/cabinet /data/config \
 && chown -R publisher:publisher /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY grokbot_publisher ./grokbot_publisher
RUN pip install --no-cache-dir .

ENV GROKBOT_CABINET=/data/cabinet \
    PUBLISHER_TOKENS_FILE=/data/config/tokens.json \
    PYTHONUNBUFFERED=1
USER publisher
VOLUME ["/data/cabinet", "/data/config"]
ENTRYPOINT ["grokbot-publisher"]
CMD ["watch"]
