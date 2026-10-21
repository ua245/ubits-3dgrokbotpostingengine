#!/usr/bin/env sh
# Generates a 6-second vertical test clip (launch.mp4) for the example card, so
# `grokbot-publisher preview` works without your real media. Replace it with the
# real video before posting anything.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/grokbot-cabinet/approved/20261021-1849-ubits-launch/launch.mp4"
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=1080x1920:rate=30:duration=6" \
  -f lavfi -i "sine=frequency=440:duration=6" \
  -c:v libx264 -pix_fmt yuv420p -preset veryfast -c:a aac -b:a 128k -shortest \
  -movflags +faststart "$out"
echo "wrote $out"
