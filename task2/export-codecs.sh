#!/bin/bash
set -euo pipefail

# Usage:
#   ./export-codecs.sh <input_file> <output_root> <codec>
#
# Codecs:
#   h264 (alias: avc)        libx264    -> <output_root>/avc/<bitrate>.mp4
#   hevc (alias: h265)        libx265    -> <output_root>/hevc/<bitrate>.mp4
#   vvc  (alias: h266)        libvvenc  -> <output_root>/vvc/<bitrate>.mkv
#   av1                       libsvtav1 -> <output_root>/av1/<bitrate>.mp4
#
# Example:
#   ./export-codecs.sh input.mp4 converted h264
#   ./export-codecs.sh input.mp4 converted hevc
#
# Each bitrate is encoded in its own ffmpeg process and measured separately
# with CodeCarbon. The emissions CSV is written next to the encoded files:
#   <output_root>/<codec>/emissions.csv

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <input_file> <output_root> <codec>" >&2
  echo "Supported codecs: h264, hevc, vvc, av1" >&2
  echo "Example: $0 input.mp4 converted h264" >&2
  exit 1
fi

INPUT_FILE="$1"
OUTPUT_ROOT="$2"
CODEC_ARG="${3,,}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BITRATES=(200k 500k 1M 2M 4M)
COUNTRY_ISO_CODE="${COUNTRY_ISO_CODE:-AUT}"

case "$CODEC_ARG" in
  h264|avc)
    OUTPUT_SUBDIR="avc"
    PROJECT_PREFIX="h264"
    ENCODER="libx264"
    EXTENSION="mp4"
    ;;
  hevc|h265)
    OUTPUT_SUBDIR="hevc"
    PROJECT_PREFIX="hevc"
    ENCODER="libx265"
    EXTENSION="mp4"
    ;;
  vvc|h266)
    OUTPUT_SUBDIR="vvc"
    PROJECT_PREFIX="vvc"
    ENCODER="libvvenc"
    EXTENSION="mkv"
    ;;
  av1)
    OUTPUT_SUBDIR="av1"
    PROJECT_PREFIX="av1"
    ENCODER="libsvtav1"
    EXTENSION="mp4"
    ;;
  *)
    echo "Unsupported codec: $CODEC_ARG" >&2
    echo "Supported codecs: h264, hevc, vvc, av1" >&2
    exit 1
    ;;
esac

if [ ! -f "$INPUT_FILE" ]; then
  echo "Input file not found: $INPUT_FILE" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found." >&2
  exit 1
fi

if ! ffmpeg -hide_banner -encoders 2>/dev/null \
  | awk -v encoder="$ENCODER" '$2 == encoder { found = 1 } END { exit !found }'; then
  echo "ffmpeg does not provide the '$ENCODER' encoder needed for $CODEC_ARG." >&2
  echo "Install an ffmpeg build with $ENCODER support." >&2
  exit 1
fi

if [ -x "$SCRIPT_DIR/.venv/bin/codecarbon" ]; then
  CODECARBON="$SCRIPT_DIR/.venv/bin/codecarbon"
elif command -v codecarbon >/dev/null 2>&1; then
  CODECARBON="$(command -v codecarbon)"
else
  echo "codecarbon not found. Create the venv first:" >&2
  echo "  python3 -m venv \"$SCRIPT_DIR/.venv\"" >&2
  echo "  \"$SCRIPT_DIR/.venv/bin/pip\" install -r \"$SCRIPT_DIR/requirements.txt\"" >&2
  exit 1
fi

OUTPUT_DIR="$OUTPUT_ROOT/$OUTPUT_SUBDIR"
mkdir -p "$OUTPUT_DIR"
export CODECARBON_OUTPUT_DIR="$OUTPUT_DIR"

for bitrate in "${BITRATES[@]}"; do
  echo "Encoding $bitrate with $ENCODER ($CODEC_ARG)..."
  CODECARBON_PROJECT_NAME="$PROJECT_PREFIX-$bitrate" "$CODECARBON" monitor \
    --no-api --offline --country-iso-code "$COUNTRY_ISO_CODE" -- \
    ffmpeg -y -i "$INPUT_FILE" \
      -map 0:v:0 -map 0:a:0? \
      -c:v "$ENCODER" -b:v "$bitrate" \
      -c:a copy \
      "$OUTPUT_DIR/$bitrate.$EXTENSION"
done

echo "Done. Carbon results: $OUTPUT_DIR/emissions.csv"
