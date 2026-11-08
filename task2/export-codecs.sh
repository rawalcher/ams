#!/bin/bash
set -euo pipefail

# Usage:
#   ./export-codecs.sh <input_file> <output_root> <codec> [mode]
#
# Modes:
#   all     encode all bitrates and measure decoding (default)
#   encode  only encode the bitrates
#   decode  only measure decoding of already encoded bitrates
#
# Codecs:
#   h264 (alias: avc)        libx264    -> <output_root>/avc/<bitrate>.mp4
#   hevc (alias: h265)        libx265    -> <output_root>/hevc/<bitrate>.mp4
#   vvc  (alias: h266)        vvencapp   -> <output_root>/vvc/<bitrate>.mkv
#                                         (raw bitstream: <bitrate>.266)
#   av1                       libsvtav1 -> <output_root>/av1/<bitrate>.mp4
#
# Example:
#   ./export-codecs.sh input.mp4 converted h264
#   ./export-codecs.sh input.mp4 converted hevc
#
# Each bitrate is encoded in its own encoder process and measured separately
# with CodeCarbon. The emissions CSV is written next to the encoded files:
#   <output_root>/<codec>/emissions.csv
#
# Decoding is measured the same way and written to:
#   <output_root>/<codec>/decode_emissions.csv
#
# VVC uses the standalone vvencapp encoder and vvdecapp decoder (AUR packages
# vvenc and vvdec). The other codecs use ffmpeg's built-in encoder/decoder.

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
  echo "Usage: $0 <input_file> <output_root> <codec> [all|encode|decode]" >&2
  echo "Supported codecs: h264, hevc, vvc, av1" >&2
  echo "Example: $0 input.mp4 converted h264" >&2
  exit 1
fi

INPUT_FILE="$1"
OUTPUT_ROOT="$2"
CODEC_ARG="${3,,}"
MODE="${4:-all}"
MODE="${MODE,,}"

case "$MODE" in
  all|encode|decode) ;;
  *)
    echo "Unsupported mode: $MODE (expected all, encode, or decode)" >&2
    exit 1
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BITRATES=(200k 500k 1M 2M 4M)
COUNTRY_ISO_CODE="${COUNTRY_ISO_CODE:-AUT}"
VVENC_PRESET="${VVENC_PRESET:-faster}"
VVENC_THREADS="${VVENC_THREADS:-$(nproc)}"

case "$CODEC_ARG" in
  h264|avc)
    OUTPUT_SUBDIR="avc"
    PROJECT_PREFIX="h264"
    ENCODER="libx264"
    ENCODER_MODE="ffmpeg"
    EXTENSION="mp4"
    ;;
  hevc|h265)
    OUTPUT_SUBDIR="hevc"
    PROJECT_PREFIX="hevc"
    ENCODER="libx265"
    ENCODER_MODE="ffmpeg"
    EXTENSION="mp4"
    ;;
  vvc|h266)
    OUTPUT_SUBDIR="vvc"
    PROJECT_PREFIX="vvc"
    ENCODER="vvencapp"
    ENCODER_MODE="vvencapp"
    EXTENSION="mkv"
    ;;
  av1)
    OUTPUT_SUBDIR="av1"
    PROJECT_PREFIX="av1"
    ENCODER="libsvtav1"
    ENCODER_MODE="ffmpeg"
    EXTENSION="mp4"
    ;;
  *)
    echo "Unsupported codec: $CODEC_ARG" >&2
    echo "Supported codecs: h264, hevc, vvc, av1" >&2
    exit 1
    ;;
esac

if [ "$MODE" != "decode" ] && [ ! -f "$INPUT_FILE" ]; then
  echo "Input file not found: $INPUT_FILE" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found." >&2
  exit 1
fi

if [ "$MODE" != "decode" ]; then
  if [ "$ENCODER_MODE" = "vvencapp" ]; then
    if ! command -v "$ENCODER" >/dev/null 2>&1; then
      echo "vvencapp not found. Install VVenC (AUR package: vvenc)." >&2
      exit 1
    fi
  elif ! ffmpeg -hide_banner -encoders 2>/dev/null \
    | awk -v encoder="$ENCODER" '$2 == encoder { found = 1 } END { exit !found }'; then
    echo "ffmpeg does not provide the '$ENCODER' encoder needed for $CODEC_ARG." >&2
    echo "Install an ffmpeg build with $ENCODER support." >&2
    exit 1
  fi
fi

if [ "$ENCODER_MODE" = "vvencapp" ] \
  && [ "$MODE" != "encode" ] \
  && ! command -v vvdecapp >/dev/null 2>&1; then
  echo "vvdecapp not found. It is needed to decode VVC for measurement." >&2
  echo "Install VVdeC (AUR package: vvdec) and make vvdecapp available in PATH." >&2
  exit 1
fi

if [ "$ENCODER_MODE" = "vvencapp" ] && [ "$MODE" != "decode" ]; then
  if ! command -v ffprobe >/dev/null 2>&1; then
    echo "ffprobe not found (part of ffmpeg)." >&2
    exit 1
  fi
  IFS=',' read -r SOURCE_WIDTH SOURCE_HEIGHT SOURCE_FPS <<< "$(
    ffprobe -v error -select_streams v:0 \
      -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$INPUT_FILE"
  )"
  if [ -z "${SOURCE_WIDTH:-}" ] || [ -z "${SOURCE_HEIGHT:-}" ] \
    || [ -z "${SOURCE_FPS:-}" ]; then
    echo "Could not probe video parameters from: $INPUT_FILE" >&2
    exit 1
  fi
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

if [ "$MODE" = "all" ] || [ "$MODE" = "encode" ]; then
  for bitrate in "${BITRATES[@]}"; do
    if [ "$ENCODER_MODE" = "vvencapp" ]; then
      bitstream="$OUTPUT_DIR/$bitrate.266"

      echo "Encoding $bitrate with VVenC ($CODEC_ARG)..."
      CODECARBON_PROJECT_NAME="$PROJECT_PREFIX-$bitrate" "$CODECARBON" monitor \
        --no-api --offline --country-iso-code "$COUNTRY_ISO_CODE" -- \
        bash -o pipefail -c '
          ffmpeg -v error -nostdin -i "$1" \
            -map 0:v:0 -an -f rawvideo -pix_fmt yuv420p - |
          vvencapp -i - -s "${2}x${3}" -c yuv420 --fps "$4" \
            --preset "$5" -t "$6" -p 1 -b "$7" -aud 1 -o "$8"
        ' _ "$INPUT_FILE" "$SOURCE_WIDTH" "$SOURCE_HEIGHT" "$SOURCE_FPS" \
          "$VVENC_PRESET" "$VVENC_THREADS" "$bitrate" "$bitstream"

      fps_num="${SOURCE_FPS%/*}"
      fps_den="${SOURCE_FPS#*/}"

      # The raw VVC bitstream carries no container timestamps, so assign them
      # when muxing. vvdecapp is used for analysis and reads the .266 directly.
      ffmpeg -y -v error -framerate "$SOURCE_FPS" -i "$bitstream" \
        -i "$INPUT_FILE" \
        -map 0:v:0 -map "1:a:0?" -c copy \
        -bsf:v "setts=pts=N:dts=N:time_base=${fps_den}/${fps_num}:prescale=1" \
        -shortest "$OUTPUT_DIR/$bitrate.$EXTENSION"
    else
      echo "Encoding $bitrate with $ENCODER ($CODEC_ARG)..."
      CODECARBON_PROJECT_NAME="$PROJECT_PREFIX-$bitrate" "$CODECARBON" monitor \
        --no-api --offline --country-iso-code "$COUNTRY_ISO_CODE" -- \
        ffmpeg -y -i "$INPUT_FILE" \
          -map 0:v:0 -map 0:a:0? \
          -c:v "$ENCODER" -b:v "$bitrate" \
          -c:a copy \
          "$OUTPUT_DIR/$bitrate.$EXTENSION"
    fi
  done

  echo "Done. Encode carbon results: $OUTPUT_DIR/emissions.csv"
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "decode" ]; then
  for bitrate in "${BITRATES[@]}"; do
    encoded="$OUTPUT_DIR/$bitrate.$EXTENSION"
    if [ "$ENCODER_MODE" = "vvencapp" ]; then
      decode_input="$OUTPUT_DIR/$bitrate.266"
    else
      decode_input="$encoded"
    fi

    if [ ! -f "$decode_input" ]; then
      echo "Skipping decode measurement for $bitrate: $decode_input not found." >&2
      continue
    fi

    echo "Measuring decoding of $bitrate ($CODEC_ARG)..."
    if [ "$ENCODER_MODE" = "vvencapp" ]; then
      CODECARBON_PROJECT_NAME="decode-$PROJECT_PREFIX-$bitrate" \
      CODECARBON_OUTPUT_DIR="$OUTPUT_DIR" \
      CODECARBON_OUTPUT_FILE="decode_emissions.csv" \
        "$CODECARBON" monitor \
        --no-api --offline --country-iso-code "$COUNTRY_ISO_CODE" -- \
        vvdecapp -b "$decode_input" -v 0
    else
      CODECARBON_PROJECT_NAME="decode-$PROJECT_PREFIX-$bitrate" \
      CODECARBON_OUTPUT_DIR="$OUTPUT_DIR" \
      CODECARBON_OUTPUT_FILE="decode_emissions.csv" \
        "$CODECARBON" monitor \
        --no-api --offline --country-iso-code "$COUNTRY_ISO_CODE" -- \
        ffmpeg -v error -nostdin -i "$encoded" \
          -map 0:v -f null -
    fi
  done

  echo "Done. Decode carbon results: $OUTPUT_DIR/decode_emissions.csv"
fi
