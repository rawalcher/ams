#!/bin/bash

INPUT_FILE=$1
OUTPUT_DIR=$2

if [ -z "$INPUT_FILE" ] || [ -z "$OUTPUT_DIR" ]; then
  echo "Usage: $0 <input_file> <output_directory>"
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

ffmpeg -i "$INPUT_FILE" \
  -filter_complex "[0:v]split=5[v1][v2][v3][v4][v5]" \
  -map "[v1]" -map "[v2]" -map "[v3]" -map "[v4]" -map "[v5]" \
  -map 0:a \
  -c:v libx264 \
  -force_key_frames "expr:gte(t,n_forced*2)" -sc_threshold 0 \
  -b:v:0 200k -b:v:1 500k -b:v:2 1M -b:v:3 2M -b:v:4 4M \
  -c:a copy \
  -f dash -seg_duration 2 \
  -adaptation_sets "id=0,streams=v id=1,streams=a" \
  "$OUTPUT_DIR/manifest.mpd"
