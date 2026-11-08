#!/bin/bash
set -euo pipefail

INPUT_FILE=${1:-}
OUTPUT_DIR=${2:-}

if [ -z "$INPUT_FILE" ] || [ -z "$OUTPUT_DIR" ]; then
  echo "Usage: $0 <input_file> <output_directory>" >&2
  exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
  echo "Input file not found: $INPUT_FILE" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

HLS_TIME=2

# One rendition per entry of the bitrate ladder (240p ... 1080p).
FILTER_COMPLEX="[0:v]split=5[v1][v2][v3][v4][v5];"
FILTER_COMPLEX+="[v1]scale=426:240[v1o];[v2]scale=640:360[v2o];"
FILTER_COMPLEX+="[v3]scale=854:480[v3o];[v4]scale=1280:720[v4o];"
FILTER_COMPLEX+="[v5]scale=1920:1080[v5o];"
# The HLS muxer rejects the same elementary stream in multiple variants, so
# every variant gets its own copy of the audio stream.
FILTER_COMPLEX+="[0:a:0]asplit=5[a1][a2][a3][a4][a5]"

ffmpeg -y -i "$INPUT_FILE" \
  -filter_complex "$FILTER_COMPLEX" \
  -map "[v1o]" -map "[v2o]" -map "[v3o]" -map "[v4o]" -map "[v5o]" \
  -map "[a1]" -map "[a2]" -map "[a3]" -map "[a4]" -map "[a5]" \
  -c:v libx264 -preset veryfast \
  -b:v:0 200k -b:v:1 500k -b:v:2 1M -b:v:3 2M -b:v:4 4M \
  -c:a aac -b:a 128k -ac 2 \
  -sc_threshold 0 -force_key_frames "expr:gte(t,n_forced*$HLS_TIME)" \
  -f hls \
  -hls_time "$HLS_TIME" \
  -hls_playlist_type vod \
  -hls_flags independent_segments \
  -master_pl_name master.m3u8 \
  -var_stream_map "v:0,a:0 v:1,a:1 v:2,a:2 v:3,a:3 v:4,a:4" \
  "$OUTPUT_DIR/stream_%v.m3u8"
