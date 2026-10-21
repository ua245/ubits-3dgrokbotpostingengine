# Multi-arch friendly (amd64 laptop/cloud, arm64 Switch/L4T):
#   docker build -t grokbot-publisher .
#   docker run --rm --env-file .env -v "$PWD/grokbot-cabinet:/data/cabinet" \
#     -v "$HOME/.config/grokbot-publisher:/data/config" grokbot-publisher watch
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 publisher
WORKDIR /app
COPY pyproject.toml README.md ./
COPY grokbot_publisher ./grokbot_publisher
RUN pip install --no-cache-dir .

ENV GROKBOT_CABINET=/data/cabinet \
    PUBLISHER_TOKENS_FILE=/data/config/tokens.json \
    PYTHONUNBUFFERED=1
USER publisher
ENTRYPOINT ["grokbot-publisher"]
CMD ["watch"]
