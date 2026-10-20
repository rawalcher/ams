#!/usr/bin/env python3
"""Compare PSNR, bitrate, and carbon emissions across video codecs.

Encoded videos and per-codec CodeCarbon CSV files are read from:
    <output-root>/avc/<bitrate>.mp4
    <output-root>/hevc/<bitrate>.mp4
    <output-root>/av1/<bitrate>.mp4
    <output-root>/vvc/<bitrate>.mkv
    <output-root>/<codec>/emissions.csv

When --codecs is omitted, the supported codecs (avc, hevc, av1, vvc) that
actually have encoded files under <output-root> are detected automatically.

For backwards compatibility, the directory aliases h264, h265, and h266 are
also recognized, as are AVC files directly in <output-root>, a shared
<output-root>/carbon/emissions.csv, and CodeCarbon project names starting with
"h264-".

Outputs:
    <output-root>/analysis/results.csv
    <output-root>/analysis/quality_vs_bitrate.png
    <output-root>/analysis/quality_vs_emissions.png
    <output-root>/analysis/psnr/<codec>/<bitrate>.log
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import NullFormatter, ScalarFormatter

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

# Accepted directory names for each codec. The first name is the canonical one
# written by export-codecs.sh; the others keep older runs readable.
CODEC_DIR_NAMES = {
    "avc": ("avc", "h264"),
    "hevc": ("hevc", "h265"),
    "av1": ("av1",),
    "vvc": ("vvc", "h266"),
}

MSE_AVG_RE = re.compile(r"\bmse_avg:(\S+)")
MSE_Y_RE = re.compile(r"\bmse_y:(\S+)")
PROJECT_RE = re.compile(r"^(h264|avc|hevc|av1|vvc)-(.+)$")
ENCODED_SUFFIXES = (".mp4", ".mkv", ".266")
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


def find_encoded_file(directory: Path, label: str) -> Path | None:
    """Find an encoded output by label, trying the supported containers."""
    for suffix in ENCODED_SUFFIXES:
        candidate = directory / f"{label}{suffix}"
        if candidate.exists():
            return candidate
    return None


def is_encoded_file(path: Path) -> bool:
    """Return whether a path looks like an encoded video named by bitrate."""
    return (
        path.is_file()
        and path.suffix in ENCODED_SUFFIXES
        and BITRATE_LABEL_RE.fullmatch(path.stem) is not None
    )


def codec_search_dirs(root: Path, codec: str) -> list[Path]:
    """Return the directories checked for a codec's encoded files."""
    dirs = [root / dirname for dirname in CODEC_DIR_NAMES.get(codec, (codec,))]
    # Backwards compatibility: the first AVC run wrote files directly into root
    # and used CodeCarbon project names starting with "h264-".
    if codec == "avc":
        dirs.append(root)
    return dirs


def format_search_dirs(root: Path, codec: str) -> str:
    """Describe the searched directories, relative to cwd when convenient."""
    cwd = Path.cwd().resolve()
    rendered: list[str] = []
    for path in codec_search_dirs(root, codec):
        try:
            rendered.append(str(path.relative_to(cwd)))
        except ValueError:
            rendered.append(str(path))
    return ", ".join(rendered)


def locate_codec_dir(root: Path, codec: str) -> Path | None:
    """Locate the directory containing any of a codec's encoded files."""
    for codec_dir in codec_search_dirs(root, codec):
        if codec_dir.is_dir() and any(
            is_encoded_file(path) for path in codec_dir.iterdir()
        ):
            return codec_dir
    return None


def find_emissions_files(root: Path) -> list[Path]:
    """Find CodeCarbon CSVs, including per-codec files next to the videos."""
    candidates = [root / "carbon" / "emissions.csv"]
    for dirnames in CODEC_DIR_NAMES.values():
        for dirname in dirnames:
            candidates.append(root / dirname / "emissions.csv")
            candidates.append(root / dirname / "carbon" / "emissions.csv")
    return [path for path in candidates if path.is_file()]


def run_psnr(encoded: Path, reference: Path, stats_file: Path) -> None:
    """Run ffmpeg's PSNR filter and write per-frame statistics."""
    input_args: list[str] = []
    if encoded.suffix == ".266":
        input_args = ["-f", "vvc"]

    graph = (
        "[0:v]setpts=PTS-STARTPTS,settb=AVTB,format=yuv420p[a];"
        "[1:v]setpts=PTS-STARTPTS,settb=AVTB,format=yuv420p[b];"
        f"[a][b]psnr=stats_file={stats_file.as_posix()}"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        *input_args,
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
    input_args: list[str] = []
    if path.suffix == ".266":
        input_args = ["-f", "vvc"]

    cmd = [
        "ffprobe",
        "-v",
        "error",
        *input_args,
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=bit_rate,duration,width,height",
        "-of",
        "json",
        str(path),
    ]
    data = json.loads(subprocess.check_output(cmd, text=True))
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {path}")

    stream = streams[0]
    duration = float(stream.get("duration") or 0.0)
    bitrate = float(stream.get("bit_rate") or 0.0)
    if bitrate <= 0 and duration > 0:
        # Fallback includes audio, but is better than no value.
        bitrate = path.stat().st_size * 8.0 / duration
    return bitrate / 1000.0, duration, int(stream["width"]), int(stream["height"])


def load_emissions(paths: list[Path]) -> pd.DataFrame:
    """Group CodeCarbon rows from one or more CSVs by codec and target bitrate."""
    frames: list[pd.DataFrame] = []
    for path in paths:
        emissions = pd.read_csv(path)
        required = {"project_name", "emissions", "energy_consumed", "duration"}
        missing = required - set(emissions.columns)
        if missing:
            raise RuntimeError(f"{path} is missing columns: {sorted(missing)}")
        frames.append(emissions)

    emissions = pd.concat(frames, ignore_index=True)
    if "run_id" in emissions.columns:
        emissions = emissions.drop_duplicates(subset="run_id")

    extracted = emissions["project_name"].str.extract(PROJECT_RE)
    emissions["codec"] = extracted[0].replace({"h264": "avc"})
    emissions["target"] = extracted[1]
    emissions = emissions.dropna(subset=["codec", "target"]).copy()
    emissions["emissions_g"] = emissions["emissions"].astype(float) * 1000.0

    grouped = (
        emissions.groupby(["codec", "target"], as_index=False)
        .agg(
            emissions_g=("emissions_g", "mean"),
            energy_kwh=("energy_consumed", "mean"),
            encode_seconds=("duration", "mean"),
            n_runs=("emissions_g", "size"),
        )
    )
    std = (
        emissions.groupby(["codec", "target"])["emissions_g"]
        .std(ddof=1)
        .fillna(0.0)
        .rename("emissions_g_std")
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
            "per-codec <codec>/emissions.csv"
        ),
    )
    parser.add_argument(
        "--emissions",
        type=Path,
        default=None,
        help=(
            "Explicit CodeCarbon emissions CSV (default: discover "
            "<output-root>/<codec>/emissions.csv, including legacy "
            "<output-root>/carbon/emissions.csv)"
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
            "Missing emissions CSV. Expected <output-root>/<codec>/emissions.csv "
            "or <output-root>/carbon/emissions.csv.",
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
                f"{format_search_dirs(output_root, codec)}); "
                "excluding it from analysis.",
                file=sys.stderr,
            )

    analysis_dir.mkdir(parents=True, exist_ok=True)
    psnr_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for codec, codec_dir in codec_dirs.items():
        if codec_dir is None:
            continue

        missing_labels = [
            label for label in labels if find_encoded_file(codec_dir, label) is None
        ]
        present_labels = [label for label in labels if label not in missing_labels]
        if missing_labels:
            if present_labels:
                continuation = f"continuing with {', '.join(present_labels)}"
            else:
                continuation = "no requested bitrates are available for this codec"
            print(
                f"Warning: codec '{codec}' is missing requested bitrate file(s): "
                f"{', '.join(missing_labels)} (looked in "
                f"{format_search_dirs(output_root, codec)}); {continuation}.",
                file=sys.stderr,
            )

        stats_dir = psnr_root / codec
        stats_dir.mkdir(parents=True, exist_ok=True)

        for label in labels:
            encoded = find_encoded_file(codec_dir, label)
            if encoded is None:
                continue

            stats_file = stats_dir / f"{label}.log"
            legacy_stats = psnr_root / f"{label}.log"
            if (
                codec == "avc"
                and not stats_file.exists()
                and legacy_stats.exists()
            ):
                stats_file = legacy_stats

            stats_are_current = (
                stats_file.exists()
                and stats_file.stat().st_mtime
                >= max(encoded.stat().st_mtime, source.stat().st_mtime)
            )
            if args.force or not stats_are_current:
                print(f"Computing PSNR for {codec} {label} ...", flush=True)
                run_psnr(encoded, source, stats_file)
            else:
                print(f"Reusing PSNR stats for {codec} {label}")

            mse_avg, mse_y = parse_mse(stats_file)
            actual_kbps, duration, width, height = probe_video(encoded)
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

    results_path = analysis_dir / "results.csv"
    results.to_csv(results_path, index=False)

    printable = results[
        [
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
    ].rename(
        columns={
            "actual_video_kbps": "video_kbps",
            "emissions_g_std": "emissions_std_g",
            "encode_seconds": "encode_s",
            "n_runs": "runs",
        }
    )
    print("\nResults:")
    print(printable.to_string(index=False, float_format=lambda value: f"{value:.4f}"))

    plot_quality_vs_bitrate(results, analysis_dir / "quality_vs_bitrate.png")
    if not missing_emissions.all():
        plot_quality_vs_emissions(results, analysis_dir / "quality_vs_emissions.png")
    else:
        print("Skipping emissions plot: no matching emissions data.", file=sys.stderr)

    print(f"\nWrote {results_path}")
    print(f"Wrote {analysis_dir / 'quality_vs_bitrate.png'}")
    if not missing_emissions.all():
        print(f"Wrote {analysis_dir / 'quality_vs_emissions.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
