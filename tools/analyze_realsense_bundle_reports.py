#!/usr/bin/env python3
"""Summarize the RealSense Bundle sections in existing alignment reports.

The input reports are read-only. By default, the script searches
``runtime_sessions/demos/demo_*/aligned/alignment_report.md`` and writes one
new, timestamped per-demo CSV under ``temp_realsense_test``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "runtime_sessions" / "demos"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "temp_realsense_test"
REPORT_GLOB = "demo_*/aligned/alignment_report.md"
SECTION_HEADING = "## RealSense Bundle"
NS_PER_MS = 1_000_000.0
THRESHOLDS_MS = (15.0, 18.0, 20.0, 22.0, 25.0, 30.0, 50.0)
NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


class MalformedReport(ValueError):
    """An alignment report has no valid RealSense Bundle section."""


@dataclass(frozen=True)
class SpanStats:
    median_ns: float
    p95_ns: float
    max_ns: float


@dataclass(frozen=True)
class ReportData:
    path: Path
    demo_path: Path
    demo_name: str
    bundles: int
    header: SpanStats
    recorded: SpanStats
    modes: dict[str, int]
    quality: dict[str, int]
    resync: int
    degraded: int
    reused: int
    projection_matched: int
    projection_invalid: int
    projection_mode: str


def nonnegative_int(text: str, field: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise MalformedReport(f"{field} is not an integer: {text!r}") from exc
    if value < 0:
        raise MalformedReport(f"{field} is negative: {value}")
    return value


def finite_nonnegative_float(text: str, field: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise MalformedReport(f"{field} is not numeric: {text!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise MalformedReport(f"{field} must be finite and non-negative: {text!r}")
    return value


def parse_count_object(text: str, field: str) -> dict[str, int]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise MalformedReport(f"{field} JSON contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, MalformedReport) as exc:
        if isinstance(exc, MalformedReport):
            raise
        raise MalformedReport(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MalformedReport(f"{field} JSON must be an object")
    counts: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not key:
            raise MalformedReport(f"{field} JSON keys must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise MalformedReport(
                f"{field}[{key!r}] must be a non-negative integer, got {count!r}"
            )
        counts[key] = count
    return counts


def unique_match(
    lines: list[str], pattern: re.Pattern[str], field: str
) -> re.Match[str]:
    matches = [match for line in lines if (match := pattern.fullmatch(line))]
    if len(matches) != 1:
        raise MalformedReport(
            f"expected exactly one valid {field} line, found {len(matches)}"
        )
    return matches[0]


def parse_report(path: Path) -> ReportData:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MalformedReport(f"cannot read report: {exc}") from exc

    headings = [index for index, line in enumerate(lines) if line == SECTION_HEADING]
    if len(headings) != 1:
        raise MalformedReport(
            f"expected exactly one {SECTION_HEADING!r} heading, found {len(headings)}"
        )
    start = headings[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = [line.strip() for line in lines[start:end] if line.strip()]

    patterns = {
        "Bundles": re.compile(r"Bundles:\s*(\d+)"),
        "Header span ns": re.compile(
            rf"Header span ns:\s*median=({NUMBER}),\s*p95=({NUMBER}),\s*max=({NUMBER})"
        ),
        "Recorded span ns": re.compile(
            rf"Recorded span ns:\s*median=({NUMBER}),\s*p95=({NUMBER}),\s*max=({NUMBER})"
        ),
        "Modes": re.compile(r"Modes:\s*(\{.*\})"),
        "Quality": re.compile(r"Quality:\s*(\{.*\})"),
        "Resync/degraded/reused": re.compile(
            r"Resync/degraded/reused:\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)"
        ),
        "Projection": re.compile(
            r"Projection:\s*matched=(\d+),\s*invalid=(\d+),\s*mode=([^\s,]+)"
        ),
    }
    matches = {
        field: unique_match(section, pattern, field)
        for field, pattern in patterns.items()
    }

    recognized = sum(
        1
        for line in section
        if any(pattern.fullmatch(line) for pattern in patterns.values())
    )
    if recognized != len(section):
        unexpected = [
            line
            for line in section
            if not any(pattern.fullmatch(line) for pattern in patterns.values())
        ]
        raise MalformedReport(f"unexpected line(s) in section: {unexpected!r}")

    def span(field: str) -> SpanStats:
        match = matches[field]
        return SpanStats(
            median_ns=finite_nonnegative_float(match.group(1), f"{field} median"),
            p95_ns=finite_nonnegative_float(match.group(2), f"{field} p95"),
            max_ns=finite_nonnegative_float(match.group(3), f"{field} max"),
        )

    bundles_match = matches["Bundles"]
    modes_match = matches["Modes"]
    quality_match = matches["Quality"]
    counts_match = matches["Resync/degraded/reused"]
    projection_match = matches["Projection"]
    demo_path = path.parent.parent
    return ReportData(
        path=path,
        demo_path=demo_path,
        demo_name=demo_path.name,
        bundles=nonnegative_int(bundles_match.group(1), "Bundles"),
        header=span("Header span ns"),
        recorded=span("Recorded span ns"),
        modes=parse_count_object(modes_match.group(1), "Modes"),
        quality=parse_count_object(quality_match.group(1), "Quality"),
        resync=nonnegative_int(counts_match.group(1), "resync"),
        degraded=nonnegative_int(counts_match.group(2), "degraded"),
        reused=nonnegative_int(counts_match.group(3), "reused"),
        projection_matched=nonnegative_int(
            projection_match.group(1), "Projection matched"
        ),
        projection_invalid=nonnegative_int(
            projection_match.group(2), "Projection invalid"
        ),
        projection_mode=projection_match.group(3),
    )


def percentile(values: list[float], fraction: float) -> float:
    """Return a linearly interpolated percentile (NumPy's default method)."""
    if not values:
        raise ValueError("cannot summarize an empty array")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def distribution(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "mean": fmean(values),
    }


def weighted_mean(values: list[float], weights: list[int]) -> float | None:
    total_weight = sum(weights)
    if total_weight == 0:
        return None
    return sum(value * weight for value, weight in zip(values, weights)) / total_weight


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def fmt_number(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def fmt_rate(count: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{100.0 * count / total:.4f}%"


def consistency_discrepancies(report: ReportData) -> list[str]:
    checks = [
        ("sum(Modes)", sum(report.modes.values()), report.bundles),
        ("sum(Quality)", sum(report.quality.values()), report.bundles),
        (
            "Projection matched + invalid",
            report.projection_matched + report.projection_invalid,
            report.bundles,
        ),
    ]
    discrepancies = [
        f"{display_path(report.path)}: {label}={actual}, Bundles={report.bundles}"
        for label, actual, expected in checks
        if actual != expected
    ]
    for label, span in (("Header", report.header), ("Recorded", report.recorded)):
        if not (span.median_ns <= span.p95_ns <= span.max_ns):
            discrepancies.append(
                f"{display_path(report.path)}: {label} ordering is not median <= p95 <= max"
            )
    return discrepancies


def unique_csv_path(output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_dir / f"realsense_bundle_report_analysis_{stamp}.csv"
    if not base.exists():
        return base
    for suffix in range(1, 100):
        candidate = output_dir / f"realsense_bundle_report_analysis_{stamp}_{suffix:02d}.csv"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not choose a unique CSV path under {output_dir}")


def write_csv(path: Path, reports: list[ReportData]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing CSV: {path}")
    mode_keys = sorted({key for report in reports for key in report.modes})
    quality_keys = sorted({key for report in reports for key in report.quality})
    fields = [
        "report_path",
        "demo_path",
        "demo_name",
        "bundles",
        "header_median_ns",
        "header_p95_ns",
        "header_max_ns",
        "header_median_ms",
        "header_p95_ms",
        "header_max_ms",
        "recorded_median_ns",
        "recorded_p95_ns",
        "recorded_max_ns",
        "recorded_median_ms",
        "recorded_p95_ms",
        "recorded_max_ms",
        "resync",
        "degraded",
        "reused",
        "projection_matched",
        "projection_invalid",
        "projection_mode",
        "modes_json",
        "quality_json",
        *[f"mode__{key}" for key in mode_keys],
        *[f"quality__{key}" for key in quality_keys],
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            row: dict[str, object] = {
                "report_path": display_path(report.path),
                "demo_path": display_path(report.demo_path),
                "demo_name": report.demo_name,
                "bundles": report.bundles,
                "header_median_ns": report.header.median_ns,
                "header_p95_ns": report.header.p95_ns,
                "header_max_ns": report.header.max_ns,
                "header_median_ms": report.header.median_ns / NS_PER_MS,
                "header_p95_ms": report.header.p95_ns / NS_PER_MS,
                "header_max_ms": report.header.max_ns / NS_PER_MS,
                "recorded_median_ns": report.recorded.median_ns,
                "recorded_p95_ns": report.recorded.p95_ns,
                "recorded_max_ns": report.recorded.max_ns,
                "recorded_median_ms": report.recorded.median_ns / NS_PER_MS,
                "recorded_p95_ms": report.recorded.p95_ns / NS_PER_MS,
                "recorded_max_ms": report.recorded.max_ns / NS_PER_MS,
                "resync": report.resync,
                "degraded": report.degraded,
                "reused": report.reused,
                "projection_matched": report.projection_matched,
                "projection_invalid": report.projection_invalid,
                "projection_mode": report.projection_mode,
                "modes_json": json.dumps(report.modes, sort_keys=True),
                "quality_json": json.dumps(report.quality, sort_keys=True),
            }
            row.update({f"mode__{key}": report.modes.get(key, "") for key in mode_keys})
            row.update(
                {f"quality__{key}": report.quality.get(key, "") for key in quality_keys}
            )
            writer.writerow(row)


def print_distribution_table(
    title: str,
    reports: list[ReportData],
    selector: Callable[[ReportData], SpanStats],
) -> None:
    print(f"\n{title} across demos (ms)")
    print(
        "reported metric | min | p50 | p90 | p95 | p99 | max | mean | "
        "bundle-weighted mean of per-demo metric"
    )
    print("-" * 108)
    weights = [report.bundles for report in reports]
    for label, attribute in (("median", "median_ns"), ("p95", "p95_ns"), ("max", "max_ns")):
        values = [getattr(selector(report), attribute) / NS_PER_MS for report in reports]
        stats = distribution(values)
        weighted = weighted_mean(values, weights)
        columns = " | ".join(fmt_number(stats[key]) for key in ("min", "p50", "p90", "p95", "p99", "max", "mean"))
        print(f"per-demo {label:<6} | {columns} | {fmt_number(weighted)}")
    print(
        "The last column is the bundle-weighted mean of the per-demo reported metric; "
        "it is not a global bundle-level percentile."
    )


def print_category_counts(
    title: str,
    totals: Counter[str],
    total_bundles: int,
    preferred_order: tuple[str, ...],
) -> None:
    print(f"\n{title}")
    ordered = [key for key in preferred_order if key in totals]
    ordered.extend(sorted(key for key in totals if key not in preferred_order))
    for key in ordered:
        print(f"  {key}: {totals[key]} ({fmt_rate(totals[key], total_bundles)} of Bundles)")


def threshold_rows(
    reports: list[ReportData], selector: Callable[[ReportData], SpanStats]
) -> list[tuple[float, int, int, int, int]]:
    rows = []
    for threshold in THRESHOLDS_MS:
        p95_pass = [
            report for report in reports if selector(report).p95_ns / NS_PER_MS <= threshold
        ]
        max_pass = [
            report for report in reports if selector(report).max_ns / NS_PER_MS <= threshold
        ]
        rows.append(
            (
                threshold,
                len(p95_pass),
                sum(report.bundles for report in p95_pass),
                len(max_pass),
                sum(report.bundles for report in max_pass),
            )
        )
    return rows


def print_threshold_table(
    title: str,
    reports: list[ReportData],
    selector: Callable[[ReportData], SpanStats],
) -> None:
    total_demos = len(reports)
    total_bundles = sum(report.bundles for report in reports)
    print(f"\n{title}")
    print(
        "threshold | demos p95<= | bundle-weighted demo pass | demos max<= | "
        "bundle-weighted demo pass"
    )
    print("-" * 98)
    for threshold, p95_demos, p95_bundles, max_demos, max_bundles in threshold_rows(
        reports, selector
    ):
        print(
            f"{threshold:>6.0f} ms | "
            f"{p95_demos:>3}/{total_demos:<3} ({fmt_rate(p95_demos, total_demos):>9}) | "
            f"{fmt_rate(p95_bundles, total_bundles):>9} | "
            f"{max_demos:>3}/{total_demos:<3} ({fmt_rate(max_demos, total_demos):>9}) | "
            f"{fmt_rate(max_bundles, total_bundles):>9}"
        )
    print(
        "Bundle-weighted columns weight each whole demo by Bundles; they do not estimate "
        "the fraction of individual bundles below the threshold."
    )


def print_interpretation(reports: list[ReportData]) -> None:
    total_demos = len(reports)
    total_bundles = sum(report.bundles for report in reports)
    header_rows = {row[0]: row for row in threshold_rows(reports, lambda report: report.header)}
    row20 = header_rows[20.0]
    row50 = header_rows[50.0]
    header_p95 = [report.header.p95_ns / NS_PER_MS for report in reports]
    header_max = [report.header.max_ns / NS_PER_MS for report in reports]
    header_p95_weighted = weighted_mean(
        header_p95, [report.bundles for report in reports]
    )

    all_max_below_50 = row50[3] == total_demos
    if all_max_below_50:
        hard_gate_assessment = "comfortably above every reported historical Header max"
    elif row50[3] / total_demos >= 0.99:
        hard_gate_assessment = "comfortably above normal historical Header max values, with outlier(s)"
    else:
        hard_gate_assessment = "not comfortably above the historical Header max distribution"

    print("\nThreshold interpretation")
    print(
        f"1. 20 ms nominal source-span threshold: the across-demo p50 of reported Header "
        f"p95 is {percentile(header_p95, 0.50):.3f} ms, and its bundle-weighted per-demo "
        f"mean is {fmt_number(header_p95_weighted)} ms. "
        f"That makes 20 ms reasonable as a nominal historical target/reference. However, "
        f"only {row20[1]}/{total_demos} demos "
        f"({fmt_rate(row20[1], total_demos)}) have Header p95 <= 20 ms, and "
        f"{row20[3]}/{total_demos} ({fmt_rate(row20[3], total_demos)}) have Header max "
        f"<= 20 ms. The bundle-weighted demo-pass rates are "
        f"{fmt_rate(row20[2], total_bundles)} and {fmt_rate(row20[4], total_bundles)}, "
        "respectively, so 20 ms is not a comfortably inclusive limit or an all-demo hard "
        "bound."
    )
    print(
        f"2. 50 ms hard source-span gate: {row50[3]}/{total_demos} demos have Header max "
        f"<= 50 ms. The across-demo Header max p99 is "
        f"{percentile(header_max, 0.99):.3f} ms and the largest reported Header max is "
        f"{max(header_max):.3f} ms, so 50 ms is {hard_gate_assessment}."
    )
    print(
        "3. Provisional bring-up only: use camera_bundle_wait_ms=50 ms initially, with "
        "30-50 ms as a conservative exploration range. This is an operational placeholder, "
        "not a calibrated arrival-time threshold and not an inference from the Recorded span. "
        f"For context only, the across-demo p95 of per-demo Header p95 is "
        f"{percentile(header_p95, 0.95):.3f} ms."
    )
    print(
        "These reports cannot determine the final camera_bundle_wait_ms. Final calibration "
        "must use SensorHub CameraSample ingest_monotonic_ns traces under the actual workload: "
        "RealSense + intra-process SensorHub + rosbag + policy inference + ZMQ/telemetry."
    )


def print_summary(
    paths: list[Path],
    reports: list[ReportData],
    errors: list[tuple[Path, str]],
    csv_path: Path | None,
) -> None:
    total_bundles = sum(report.bundles for report in reports)
    print("RealSense Bundle alignment-report analysis")
    print(f"Reports matched: {len(paths)}")
    print(f"Parsed successfully: {len(reports)}")
    print(f"Malformed/error reports: {len(errors)}")
    for path, error in errors:
        print(f"  {display_path(path)}: {error}")
    print(f"Total Bundles (successfully parsed reports): {total_bundles}")
    if csv_path is not None:
        print(f"Per-demo CSV: {display_path(csv_path)}")
    if not reports:
        return

    print(
        "\nScope limitation: the reports contain only per-demo summary statistics. An exact "
        "global bundle-level median or p95 cannot be reconstructed."
    )
    print(
        "Header span is the historical source-time coherence measure. Recorded span includes "
        "the old ROS/rosbag recording path and is only a historical/stress reference; it is "
        "not the future SensorHub intra-process arrival-time distribution."
    )

    print_distribution_table("Header span", reports, lambda report: report.header)
    print_distribution_table(
        "Recorded span (historical recorder-path reference only)",
        reports,
        lambda report: report.recorded,
    )

    mode_totals: Counter[str] = Counter()
    quality_totals: Counter[str] = Counter()
    for report in reports:
        mode_totals.update(report.modes)
        quality_totals.update(report.quality)
    print_category_counts(
        "Modes (aggregated)",
        mode_totals,
        total_bundles,
        ("initial_search", "fallback_search", "locked_plus_one"),
    )
    print_category_counts(
        "Quality (aggregated)",
        quality_totals,
        total_bundles,
        ("degraded_span", "ok", "invalid_timestamp_mismatch"),
    )

    print("\nOther counts (aggregated)")
    for label, count in (
        ("resync", sum(report.resync for report in reports)),
        ("degraded", sum(report.degraded for report in reports)),
        ("reused", sum(report.reused for report in reports)),
        ("Projection matched", sum(report.projection_matched for report in reports)),
        ("Projection invalid", sum(report.projection_invalid for report in reports)),
    ):
        print(f"  {label}: {count} ({fmt_rate(count, total_bundles)} of Bundles)")

    projection_demo_modes = Counter(report.projection_mode for report in reports)
    projection_bundle_modes: Counter[str] = Counter()
    for report in reports:
        projection_bundle_modes[report.projection_mode] += report.bundles
    print("\nProjection modes")
    for mode in sorted(projection_demo_modes):
        print(
            f"  {mode}: {projection_demo_modes[mode]} demos; "
            f"{projection_bundle_modes[mode]} Bundles by whole-demo mode "
            f"({fmt_rate(projection_bundle_modes[mode], total_bundles)})"
        )

    discrepancies = [
        discrepancy
        for report in reports
        for discrepancy in consistency_discrepancies(report)
    ]
    print(f"\nConsistency discrepancies: {len(discrepancies)}")
    for discrepancy in discrepancies:
        print(f"  {discrepancy}")

    print_threshold_table(
        "Header/source span candidate thresholds", reports, lambda report: report.header
    )
    print_threshold_table(
        "Recorded span candidate thresholds (historical recorder-path reference only; "
        "not camera_bundle_wait_ms evidence)",
        reports,
        lambda report: report.recorded,
    )
    print_interpretation(reports)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"demo root containing {REPORT_GLOB} (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="per-demo CSV path; existing files are never overwritten",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for the timestamped CSV when --csv is omitted",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = sorted(args.input_dir.glob(REPORT_GLOB))
    reports: list[ReportData] = []
    errors: list[tuple[Path, str]] = []
    for path in paths:
        try:
            reports.append(parse_report(path))
        except MalformedReport as exc:
            errors.append((path, str(exc)))

    csv_path: Path | None = None
    if reports:
        csv_path = args.csv if args.csv is not None else unique_csv_path(args.output_dir)
        try:
            write_csv(csv_path, reports)
        except OSError as exc:
            print(f"ERROR: could not write per-demo CSV {csv_path}: {exc}")
            return 2

    print_summary(paths, reports, errors, csv_path)
    if not paths:
        print(f"ERROR: no reports matched {args.input_dir / REPORT_GLOB}")
        return 1
    if not reports:
        print("ERROR: no reports parsed successfully")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
