#!/usr/bin/env python3
"""Compare PSNR, bitrate, and carbon emissions across video codecs.

Encoded videos and per-codec CodeCarbon CSV files are read from the layout
produced by export-codecs.sh:
    <output-root>/avc/<bitrate>.mp4
    <output-root>/hevc/<bitrate>.mp4
    <output-root>/av1/<bitrate>.mp4
    <output-root>/vvc/<bitrate>.mkv
    <output-root>/vvc/<bitrate>.266
    <output-root>/<codec>/emissions.csv
    <output-root>/<codec>/decode_emissions.csv

VVC is encoded with the standalone vvencapp and decoded for PSNR with
vvdecapp (AUR packages: vvenc and vvdec).

When --codecs is omitted, the supported codecs (avc, hevc, av1, vvc) that
actually have encoded files under <output-root> are detected automatically.

Outputs:
    <output-root>/analysis/results.csv
    <output-root>/analysis/quality_vs_bitrate.png
    <output-root>/analysis/quality_vs_emissions.png
    <output-root>/analysis/encode_time_vs_bitrate.png
    <output-root>/analysis/decode_time_vs_bitrate.png
    <output-root>/analysis/decode_energy_vs_bitrate.png
    <output-root>/analysis/psnr/<codec>/<bitrate>.log
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import NullFormatter

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

CODECS = {
    "avc": {
        "label": "AVC (x264)",
        "marker": "o",
        "color": "#1f77b4",
    },
    "hevc": {
        "label": "HEVC (x265)",
        "marker": "s",
        "color": "#ff7f0e",
    },
    "av1": {
        "label": "AV1 (SVT-AV1)",
        "marker": "^",
        "color": "#2ca02c",
    },
    "vvc": {
        "label": "VVC (VVenC)",
        "marker": "D",
        "color": "#d62728",
    },
}

# Mapping to the values used by export-codecs.sh. The AVC encoder is exposed as
# "h264" in CodeCarbon project names even though its output directory is "avc".
CODEC_PROJECT_PREFIXES = {
    "avc": "h264",
    "hevc": "hevc",
    "av1": "av1",
    "vvc": "vvc",
}

CODEC_EXTENSIONS = {
    "avc": ".mp4",
    "hevc": ".mp4",
    "av1": ".mp4",
    "vvc": ".mkv",
}

MSE_AVG_RE = re.compile(r"\bmse_avg:(\S+)")
MSE_Y_RE = re.compile(r"\bmse_y:(\S+)")
PROJECT_RE = re.compile(
    r"^("
    + "|".join(re.escape(prefix) for prefix in CODEC_PROJECT_PREFIXES.values())
    + r")-(.+)$"
)
DECODE_PROJECT_RE = re.compile(
    r"^decode-("
    + "|".join(re.escape(prefix) for prefix in CODEC_PROJECT_PREFIXES.values())
    + r")-(.+)$"
)
PROJECT_CODECS = {
    prefix: codec for codec, prefix in CODEC_PROJECT_PREFIXES.items()
}
BITRATE_LABEL_RE = re.compile(r"\d+(?:\.\d+)?[kKmM]?")


def bitrate_label_to_kbps(label: str) -> int:
    """Convert labels such as '200k', '1M', or '500000' to kbps."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", label.strip())
    if not match:
        raise ValueError(f"Invalid bitrate label: {label!r}")
    value = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "m":
        value *= 1000
    elif suffix == "k":
        pass
    elif value >= 1000:
        # A bare value like 500000 is bits/s, not kbps.
        value /= 1000
    return int(round(value))


def find_encoded_file(directory: Path, label: str, codec: str) -> Path | None:
    """Find a codec's encoded output by bitrate label."""
    candidate = directory / f"{label}{CODEC_EXTENSIONS[codec]}"
    return candidate if candidate.exists() else None


def find_vvc_bitstream(directory: Path, label: str) -> Path | None:
    """Find the raw VVC bitstream written next to a VVC container."""
    candidate = directory / f"{label}.266"
    return candidate if candidate.exists() else None


def is_encoded_file(path: Path, codec: str) -> bool:
    """Return whether a path looks like a codec's bitrate-named output."""
    return (
        path.is_file()
        and path.suffix == CODEC_EXTENSIONS[codec]
        and BITRATE_LABEL_RE.fullmatch(path.stem) is not None
    )


def locate_codec_dir(root: Path, codec: str) -> Path | None:
    """Locate the directory containing a codec's encoded files."""
    codec_dir = root / codec
    if codec_dir.is_dir() and any(
        is_encoded_file(path, codec) for path in codec_dir.iterdir()
    ):
        return codec_dir
    return None


def find_emissions_files(root: Path) -> list[Path]:
    """Find the per-codec CodeCarbon CSVs written by export-codecs.sh."""
    candidates = [root / codec / "emissions.csv" for codec in CODECS]
    return [path for path in candidates if path.is_file()]


def find_decode_emissions_files(root: Path) -> list[Path]:
    """Find the per-codec CodeCarbon decode CSVs."""
    candidates = [root / codec / "decode_emissions.csv" for codec in CODECS]
    return [path for path in candidates if path.is_file()]


def vvc_lead_frames(reference: Path) -> int:
    """Return the number of frames ffmpeg duplicates at the start for VVC.

    The VVC encode pipeline feeds rawvideo from ffmpeg into vvencapp. For
    sources with a non-zero start time, ffmpeg's frame sync inserts duplicate
    frames at the beginning. Those frames are not present in the reference
    timeline, so they must be dropped before PSNR comparison.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=start_time,avg_frame_rate",
        "-of",
        "json",
        str(reference),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    streams = data.get("streams", [])
    if not streams:
        return 0

    stream = streams[0]
    try:
        start_time = float(stream.get("start_time") or 0.0)
        num, den = (int(part) for part in stream["avg_frame_rate"].split("/"))
        fps = num / den if den else 0.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0

    if start_time <= 0 or fps <= 0:
        return 0
    return max(0, int(round(start_time * fps)))


def run_psnr(
    encoded: Path, reference: Path, stats_file: Path, *, codec: str
) -> None:
    """Run ffmpeg's PSNR filter and write per-frame statistics.

    VVC is decoded with vvdecapp because ffmpeg's native VVC decoder used by
    this build does not reorder B-frames correctly. vvdecapp writes Y4M to
    stdout, which ffmpeg reads as the first input.
    """
    graph = (
        "[0:v]setpts=PTS-STARTPTS,settb=AVTB,format=yuv420p[a];"
        "[1:v]setpts=PTS-STARTPTS,settb=AVTB,format=yuv420p[b];"
        f"[a][b]psnr=stats_file={stats_file.as_posix()}"
    )

    if codec == "vvc":
        lead_frames = vvc_lead_frames(reference)
        vvc_graph = (
            f"[0:v]trim=start_frame={lead_frames},setpts=PTS-STARTPTS,"
            "settb=AVTB,format=yuv420p[a];"
            "[1:v]setpts=PTS-STARTPTS,settb=AVTB,format=yuv420p[b];"
            f"[a][b]psnr=stats_file={stats_file.as_posix()}"
        )
        decoder = subprocess.Popen(
            ["vvdecapp", "-b", str(encoded), "-o", "-", "--y4m", "-v", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-v",
            "error",
            "-i",
            "-",
            "-i",
            str(reference),
            "-an",
            "-lavfi",
            vvc_graph,
            "-f",
            "null",
            "-",
        ]
        try:
            subprocess.run(cmd, stdin=decoder.stdout, check=True)
        finally:
            if decoder.stdout is not None:
                decoder.stdout.close()
            return_code = decoder.wait()
        if return_code != 0:
            raise RuntimeError(
                f"vvdecapp failed to decode {encoded} (exit code {return_code})"
            )
        return

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-i",
        str(encoded),
        "-i",
        str(reference),
        "-an",
        "-lavfi",
        graph,
        "-f",
        "null",
        "-",
    ]
    subprocess.run(cmd, check=True)


def parse_mse(stats_file: Path) -> tuple[float, float]:
    """Return the mean of per-frame average MSE and luma MSE."""
    mse_avg: list[float] = []
    mse_y: list[float] = []

    with stats_file.open(encoding="utf-8") as handle:
        for line in handle:
            match_avg = MSE_AVG_RE.search(line)
            if not match_avg:
                continue
            try:
                value_avg = float(match_avg.group(1))
            except ValueError:
                continue
            if math.isfinite(value_avg):
                mse_avg.append(value_avg)

            match_y = MSE_Y_RE.search(line)
            if match_y:
                try:
                    value_y = float(match_y.group(1))
                except ValueError:
                    continue
                if math.isfinite(value_y):
                    mse_y.append(value_y)

    if not mse_avg:
        raise RuntimeError(f"No MSE values found in {stats_file}")

    mean_avg = sum(mse_avg) / len(mse_avg)
    mean_y = sum(mse_y) / len(mse_y) if mse_y else mean_avg
    return mean_avg, mean_y


def mse_to_psnr(mse: float, peak: float = 255.0) -> float:
    """Convert MSE to PSNR in dB for 8-bit video."""
    if mse <= 0:
        return float("inf")
    return 10.0 * math.log10((peak * peak) / mse)


def probe_video(path: Path) -> tuple[float, float, int, int]:
    """Return video bitrate (kbps), duration (s), width, and height."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=bit_rate,duration,width,height:format=duration,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {path}")

    stream = streams[0]
    format_info = data.get("format", {})
    duration = float(
        stream.get("duration") or format_info.get("duration") or 0.0
    )
    bitrate = float(stream.get("bit_rate") or format_info.get("bit_rate") or 0.0)
    if bitrate <= 0 and duration > 0:
        # Fallback includes audio, but is better than no value.
        bitrate = path.stat().st_size * 8.0 / duration
    return bitrate / 1000.0, duration, int(stream["width"]), int(stream["height"])


def load_emissions(paths: list[Path], *, decode: bool = False) -> pd.DataFrame:
    """Group CodeCarbon rows from one or more CSVs by codec and target bitrate.

    With ``decode=True`` the rows are expected to come from
    ``decode_emissions.csv`` and the resulting value columns are prefixed with
    ``decode_`` so they can be merged with the encode measurements.
    """
    frames: list[pd.DataFrame] = []
    for path in paths:
        emissions = pd.read_csv(path)
        required = {"project_name", "emissions", "energy_consumed", "duration"}
        missing = required - set(emissions.columns)
        if missing:
            raise RuntimeError(f"{path} is missing columns: {sorted(missing)}")
        frames.append(emissions)

    if not frames:
        return pd.DataFrame(columns=["codec", "target"])

    emissions = pd.concat(frames, ignore_index=True)
    if "run_id" in emissions.columns:
        emissions = emissions.drop_duplicates(subset="run_id")

    project_re = DECODE_PROJECT_RE if decode else PROJECT_RE
    extracted = emissions["project_name"].str.extract(project_re)
    emissions["codec"] = extracted[0].map(PROJECT_CODECS)
    emissions["target"] = extracted[1]
    emissions = emissions.dropna(subset=["codec", "target"]).copy()
    emissions["emissions_g"] = emissions["emissions"].astype(float) * 1000.0

    prefix = "decode_" if decode else ""
    seconds_col = "decode_seconds" if decode else "encode_seconds"
    grouped = (
        emissions.groupby(["codec", "target"], as_index=False)
        .agg(
            **{
                f"{prefix}emissions_g": ("emissions_g", "mean"),
                f"{prefix}energy_kwh": ("energy_consumed", "mean"),
                seconds_col: ("duration", "mean"),
                f"{prefix}n_runs": ("emissions_g", "size"),
            }
        )
    )
    std = (
        emissions.groupby(["codec", "target"])["emissions_g"]
        .std(ddof=1)
        .fillna(0.0)
        .rename(f"{prefix}emissions_g_std")
        .reset_index()
    )
    return grouped.merge(std, on=["codec", "target"], how="left")


def plot_quality_vs_bitrate(results: pd.DataFrame, output: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.6, 5.2))

    for codec in CODECS:
        group = results[results["codec"] == codec].sort_values("actual_video_kbps")
        if group.empty:
            continue
        style = CODECS[codec]
        ax.plot(
            group["actual_video_kbps"],
            group["psnr_db"],
            marker=style["marker"],
            color=style["color"],
            linewidth=1.8,
            markersize=6,
            label=style["label"],
        )

    ticks = sorted(results["target_kbps"].unique())
    ax.set_xscale("log")
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(value) for value in ticks])
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("Video bitrate (kbps, log scale)")
    ax.set_ylabel("PSNR (dB)")
    ax.set_title("Quality vs. bitrate")
    ax.grid(True, which="major", alpha=0.3)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=300)
    plt.close(fig)


def plot_quality_vs_emissions(results: pd.DataFrame, output: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.6, 5.2))

    for codec in CODECS:
        group = results[results["codec"] == codec].sort_values("emissions_g")
        if group.empty:
            continue
        style = CODECS[codec]
        ax.errorbar(
            group["emissions_g"],
            group["psnr_db"],
            xerr=group["emissions_g_std"],
            marker=style["marker"],
            color=style["color"],
            linewidth=1.8,
            markersize=6,
            capsize=3,
            label=style["label"],
        )

    ax.set_xlabel("Emissions (g CO$_2$eq)")
    ax.set_ylabel("PSNR (dB)")
    ax.set_title("Quality vs. carbon emissions")
    ax.grid(True, alpha=0.3)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=300)
    plt.close(fig)


def plot_metric_vs_bitrate(
    results: pd.DataFrame,
    metric: str,
    ylabel: str,
    title: str,
    output: Path,
) -> None:
    """Plot a per-codec metric against the target bitrate ladder."""
    fig, ax = plt.subplots(figsize=(8.6, 5.2))

    for codec in CODECS:
        group = results[results["codec"] == codec].dropna(subset=[metric])
        group = group.sort_values("target_kbps")
        if group.empty:
            continue
        style = CODECS[codec]
        ax.plot(
            group["target_kbps"],
            group[metric],
            marker=style["marker"],
            color=style["color"],
            linewidth=1.8,
            markersize=6,
            label=style["label"],
        )

    ticks = sorted(results["target_kbps"].unique())
    ax.set_xscale("log")
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(value) for value in ticks])
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("Target video bitrate (kbps, log scale)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, which="major", alpha=0.3)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=300)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT / "bbb_sunflower_1080p_60fps_normal.mp4",
        help="Reference/source video",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=SCRIPT_DIR / "converted",
        help=(
            "Root containing {avc,hevc,av1,vvc}/<bitrate>.mp4|.mkv and "
            "per-codec <codec>/emissions.csv and decode_emissions.csv "
            "(layout produced by export-codecs.sh). VVC also has <bitrate>.266."
        ),
    )
    parser.add_argument(
        "--emissions",
        type=Path,
        default=None,
        help=(
            "Explicit CodeCarbon emissions CSV (default: discover "
            "<output-root>/<codec>/emissions.csv)"
        ),
    )
    parser.add_argument(
        "--bitrates",
        default="200k,500k,1M,2M,4M",
        help="Comma-separated target bitrate labels (default: 200k,500k,1M,2M,4M)",
    )
    parser.add_argument(
        "--codecs",
        default=None,
        help=(
            "Comma-separated codecs to analyze (default: auto-detect present "
            "codecs among avc, hevc, av1, vvc)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute PSNR even if per-frame stats already exist",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    output_root = args.output_root.resolve()
    emissions_paths = (
        [args.emissions.resolve()]
        if args.emissions is not None
        else find_emissions_files(output_root)
    )
    decode_emissions_paths = find_decode_emissions_files(output_root)
    analysis_dir = output_root / "analysis"
    psnr_root = analysis_dir / "psnr"
    labels = [item.strip() for item in args.bitrates.split(",") if item.strip()]

    for path, description in [
        (source, "source video"),
        (output_root, "output root"),
    ]:
        if not path.exists():
            print(f"Missing {description}: {path}", file=sys.stderr)
            return 1

    if not emissions_paths:
        print(
            "Missing emissions CSV. Expected <output-root>/<codec>/emissions.csv.",
            file=sys.stderr,
        )
        return 1

    for path in emissions_paths:
        if not path.is_file():
            print(f"Missing emissions CSV: {path}", file=sys.stderr)
            return 1

    requested_codecs = (
        [item.strip() for item in args.codecs.split(",") if item.strip()]
        if args.codecs
        else list(CODECS)
    )
    unknown = [codec for codec in requested_codecs if codec not in CODECS]
    if unknown:
        print(f"Unknown codec(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    # Detect present codecs before analyzing anything. Without --codecs this
    # covers every supported codec; with --codecs unknown names were rejected
    # above and absent ones are reported below.
    codec_dirs: dict[str, Path | None] = {}
    for codec in requested_codecs:
        if codec not in codec_dirs:
            codec_dirs[codec] = locate_codec_dir(output_root, codec)

    for codec, codec_dir in codec_dirs.items():
        if codec_dir is None:
            print(
                f"Warning: codec '{codec}' not found (looked in "
                f"{output_root / codec}); excluding it from analysis.",
                file=sys.stderr,
            )

    if any(
        codec == "vvc" and codec_dir is not None
        for codec, codec_dir in codec_dirs.items()
    ) and shutil.which("vvdecapp") is None:
        print(
            "vvdecapp not found. It is required to decode VVC for PSNR "
            "analysis (AUR package: vvdec).",
            file=sys.stderr,
        )
        return 1

    analysis_dir.mkdir(parents=True, exist_ok=True)
    psnr_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for codec, codec_dir in codec_dirs.items():
        if codec_dir is None:
            continue

        missing_labels = [
            label
            for label in labels
            if find_encoded_file(codec_dir, label, codec) is None
        ]
        present_labels = [label for label in labels if label not in missing_labels]
        if missing_labels:
            if present_labels:
                continuation = f"continuing with {', '.join(present_labels)}"
            else:
                continuation = "no requested bitrates are available for this codec"
            print(
                f"Warning: codec '{codec}' is missing requested bitrate file(s): "
                f"{', '.join(missing_labels)} (looked in {codec_dir}); "
                f"{continuation}.",
                file=sys.stderr,
            )

        stats_dir = psnr_root / codec
        stats_dir.mkdir(parents=True, exist_ok=True)

        for label in labels:
            encoded = find_encoded_file(codec_dir, label, codec)
            if encoded is None:
                continue

            bitstream = (
                find_vvc_bitstream(codec_dir, label) if codec == "vvc" else None
            )
            if codec == "vvc" and bitstream is None:
                print(
                    f"Warning: missing raw VVC bitstream for {label}; "
                    "ffmpeg's native VVC decoding may be unreliable.",
                    file=sys.stderr,
                )
            psnr_input = bitstream if bitstream is not None else encoded

            stats_file = stats_dir / f"{label}.log"
            input_mtimes = [encoded.stat().st_mtime, source.stat().st_mtime]
            if bitstream is not None:
                input_mtimes.append(bitstream.stat().st_mtime)

            stats_are_current = (
                stats_file.exists()
                and stats_file.stat().st_mtime >= max(input_mtimes)
            )
            if args.force or not stats_are_current:
                print(f"Computing PSNR for {codec} {label} ...", flush=True)
                run_psnr(psnr_input, source, stats_file, codec=codec)
            else:
                print(f"Reusing PSNR stats for {codec} {label}")

            mse_avg, mse_y = parse_mse(stats_file)
            actual_kbps, duration, width, height = probe_video(encoded)
            if bitstream is not None and duration > 0:
                # Measure the video-only bitrate from the raw VVC bitstream.
                actual_kbps = bitstream.stat().st_size * 8.0 / duration / 1000.0
            rows.append(
                {
                    "codec": codec,
                    "target": label,
                    "target_kbps": bitrate_label_to_kbps(label),
                    "file": encoded.name,
                    "actual_video_kbps": actual_kbps,
                    "width": width,
                    "height": height,
                    "duration_s": duration,
                    "psnr_db": mse_to_psnr(mse_avg),
                    "psnr_y_db": mse_to_psnr(mse_y),
                    "mse_avg": mse_avg,
                    "mse_y": mse_y,
                }
            )

    if not rows:
        print("No encoded files found to analyze.", file=sys.stderr)
        return 1

    results = pd.DataFrame(rows)
    emissions = load_emissions(emissions_paths)
    results = results.merge(emissions, on=["codec", "target"], how="left")

    decode_emissions = (
        load_emissions(decode_emissions_paths, decode=True)
        if decode_emissions_paths
        else pd.DataFrame(columns=["codec", "target"])
    )
    has_decode = "decode_seconds" in decode_emissions.columns
    if has_decode:
        results = results.merge(decode_emissions, on=["codec", "target"], how="left")
        results["decode_energy_wh"] = results["decode_energy_kwh"] * 1000.0
    else:
        print(
            "Warning: no decode emissions data found. Run "
            "export-codecs.sh ... decode to measure decoding.",
            file=sys.stderr,
        )

    codec_order = {codec: index for index, codec in enumerate(CODECS)}
    results["_codec_order"] = results["codec"].map(codec_order)
    results = (
        results.sort_values(["_codec_order", "target_kbps"])
        .drop(columns="_codec_order")
        .reset_index(drop=True)
    )

    missing_emissions = results["emissions_g"].isna()
    if missing_emissions.any():
        missing = results.loc[missing_emissions, ["codec", "target"]]
        print(
            "Warning: no emissions data for "
            + ", ".join(f"{row.codec}-{row.target}" for row in missing.itertuples()),
            file=sys.stderr,
        )

    if has_decode:
        missing_decode = results["decode_seconds"].isna()
        if missing_decode.any():
            missing = results.loc[missing_decode, ["codec", "target"]]
            print(
                "Warning: no decoding data for "
                + ", ".join(
                    f"{row.codec}-{row.target}" for row in missing.itertuples()
                ),
                file=sys.stderr,
            )

    results_path = analysis_dir / "results.csv"
    results.to_csv(results_path, index=False)

    printable_columns = [
        "codec",
        "target",
        "actual_video_kbps",
        "psnr_db",
        "psnr_y_db",
        "emissions_g",
        "emissions_g_std",
        "energy_kwh",
        "encode_seconds",
        "n_runs",
    ]
    rename = {
        "actual_video_kbps": "video_kbps",
        "emissions_g_std": "emissions_std_g",
        "encode_seconds": "encode_s",
        "n_runs": "runs",
    }
    if has_decode:
        printable_columns += [
            "decode_seconds",
            "decode_energy_wh",
            "decode_emissions_g",
            "decode_n_runs",
        ]
        rename.update(
            {
                "decode_seconds": "decode_s",
                "decode_energy_wh": "decode_energy_wh",
                "decode_emissions_g": "decode_emissions_g",
                "decode_n_runs": "decode_runs",
            }
        )

    printable = results[printable_columns].rename(columns=rename)
    print("\nResults:")
    print(printable.to_string(index=False, float_format=lambda value: f"{value:.4f}"))

    plot_quality_vs_bitrate(results, analysis_dir / "quality_vs_bitrate.png")
    if not missing_emissions.all():
        plot_quality_vs_emissions(results, analysis_dir / "quality_vs_emissions.png")
    else:
        print("Skipping emissions plot: no matching emissions data.", file=sys.stderr)

    if results["encode_seconds"].notna().any():
        plot_metric_vs_bitrate(
            results,
            "encode_seconds",
            "Encoding time (s)",
            "Encoding time vs. bitrate",
            analysis_dir / "encode_time_vs_bitrate.png",
        )
    else:
        print("Skipping encoding time plot: no encoding data.", file=sys.stderr)

    if has_decode and results["decode_seconds"].notna().any():
        plot_metric_vs_bitrate(
            results,
            "decode_seconds",
            "Decoding time (s)",
            "Decoding time vs. bitrate",
            analysis_dir / "decode_time_vs_bitrate.png",
        )
    elif not has_decode:
        print("Skipping decoding plots: no decoding data.", file=sys.stderr)

    if has_decode and results["decode_energy_wh"].notna().any():
        plot_metric_vs_bitrate(
            results,
            "decode_energy_wh",
            "Decoding energy (Wh)",
            "Decoding energy vs. bitrate",
            analysis_dir / "decode_energy_vs_bitrate.png",
        )

    print(f"\nWrote {results_path}")
    print(f"Wrote {analysis_dir / 'quality_vs_bitrate.png'}")
    if not missing_emissions.all():
        print(f"Wrote {analysis_dir / 'quality_vs_emissions.png'}")
    if results["encode_seconds"].notna().any():
        print(f"Wrote {analysis_dir / 'encode_time_vs_bitrate.png'}")
    if has_decode and results["decode_seconds"].notna().any():
        print(f"Wrote {analysis_dir / 'decode_time_vs_bitrate.png'}")
    if has_decode and results["decode_energy_wh"].notna().any():
        print(f"Wrote {analysis_dir / 'decode_energy_vs_bitrate.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
