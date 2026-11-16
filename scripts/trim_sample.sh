#!/bin/bash
# Trim provided 540M sample to ~10 min demo clip. Needs ffmpeg; stays $0 local.
set -e
SRC="/home/abhinav/Documents/github/Lingofy/samples/vidssave.com AI Crisis in Math_ Navier-Stokes, Riemann Hypothesis & Complexity Theory _ Lex Fridman Podcast #503 720P.mp4"
OUT="samples/trimmed-demo-10min.mp4"
if ! command -v ffmpeg >/dev/null; then echo "ffmpeg missing - install it, then rerun"; exit 1; fi
mkdir -p samples
ffmpeg -y -ss 0 -t 600 -i "$SRC" -c:v libx264 -crf 28 -c:a aac "$OUT"
ls -lh "$OUT"
