from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from .schemas import DashboardBucket

CSV_COLUMNS = (
    "bucket_start_utc",
    "bucket_end_utc",
    "feed_type",
    "request_count",
    "exposure_count",
    "click_count",
    "like_count",
    "share_count",
    "revisit_count",
    "dwell_ms_total",
    "dwell_ms_avg",
    "ctr",
    "active_user_count",
)


def formula_safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def dashboard_csv(buckets: Iterable[DashboardBucket]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for bucket in sorted(
        buckets,
        key=lambda row: (row.bucket_start_utc, row.bucket_end_utc, row.feed_type),
    ):
        values = bucket.model_dump(mode="json")
        writer.writerow([formula_safe(values[column]) for column in CSV_COLUMNS])
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")
