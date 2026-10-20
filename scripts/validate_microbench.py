#!/usr/bin/env python3
"""Validate complete interconnect CSV artifacts without loading Ray or CUDA."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_campaign import (  # noqa: E402
    ALLREDUCE_SIZES,
    CampaignValidationError,
    _nonnegative_float,
    _positive_float,
    _positive_integer,
    validate_allreduce_csv,
)

INTERCONNECT_FIELDS = (
    "collective", "transport", "bytes", "avg_s", "min_s",
    "algbw_GBps", "busbw_GBps",
)


def validate_interconnect_csv(
    path: str | Path, expected_transport: str | None = None
) -> None:
    source = Path(path)
    try:
        with source.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CampaignValidationError(f"cannot read interconnect artifact {source}: {exc}") from exc
    if tuple(reader.fieldnames or ()) != INTERCONNECT_FIELDS:
        raise CampaignValidationError(f"interconnect artifact {source} has the wrong header")
    sizes: dict[str, list[int]] = {"allreduce": [], "sendrecv": []}
    transports: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        context = f"{source} row {row_number}"
        if None in row or any(value is None for value in row.values()):
            raise CampaignValidationError(f"{context} has the wrong number of columns")
        collective = row["collective"]
        if collective not in sizes:
            raise CampaignValidationError(f"{context} has unsupported collective {collective!r}")
        transport = row["transport"]
        if re.fullmatch(r"roce|tcp(?:-tuned-x[1-9][0-9]*)?", transport) is None:
            raise CampaignValidationError(f"{context} has unverified transport {transport!r}")
        transports.add(transport)
        observed_mode = "roce" if transport == "roce" else "tcp"
        if expected_transport and observed_mode != expected_transport:
            raise CampaignValidationError(
                f"{context} transport {transport!r} differs from expected {expected_transport!r}"
            )
        sizes[collective].append(_positive_integer(row["bytes"], f"{context} bytes"))
        average = _positive_float(row["avg_s"], f"{context} avg_s")
        minimum = _positive_float(row["min_s"], f"{context} min_s")
        if minimum > average:
            raise CampaignValidationError(f"{context} min_s exceeds avg_s")
        for field in ("algbw_GBps", "busbw_GBps"):
            _nonnegative_float(row[field], f"{context} {field}")
    if len(transports) != 1:
        raise CampaignValidationError(f"interconnect artifact {source} must use one verified transport")
    for collective, observed in sizes.items():
        if tuple(observed) != ALLREDUCE_SIZES:
            raise CampaignValidationError(
                f"interconnect artifact {source} must contain {collective} sizes "
                "2^3 through 2^30 exactly once in order"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("allreduce", "interconnect"))
    parser.add_argument("path")
    parser.add_argument("--expected-transport", choices=("roce", "tcp"))
    args = parser.parse_args(argv)
    try:
        if args.kind == "allreduce":
            validate_allreduce_csv(args.path)
        else:
            validate_interconnect_csv(args.path, args.expected_transport)
    except (CampaignValidationError, OSError) as exc:
        print(f"validate_microbench.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
