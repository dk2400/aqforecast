#!/usr/bin/env python3
"""
Download official AirNow ozone forecasts valid in calendar year 2023 for the
Maryland reporting area currently named "Suburban DC" (historically it may
appear as "Suburban Maryland").

The script uses AirNow's ZIP-code forecast web service, which AirNow documents
as available until its planned retirement in fall 2026. It uses one ZIP solely
to identify the reporting area; the returned forecasts are reporting-area
forecasts, not ZIP-specific forecasts.

Environment:
    AIRNOW_API_KEY   Required AirNow API key.

Outputs:
    output/suburban_dc_ozone_forecasts_2023_operational.csv
    output/suburban_dc_ozone_forecasts_2023_all_leads.csv
    output/suburban_dc_ozone_forecasts_2023_strict_day_ahead.csv
    output/suburban_dc_ozone_forecasts_2023_same_day.csv
    output/suburban_dc_ozone_forecasts_2023_metadata.json

The operational file follows the stated MDE/DOEE-COG issuance schedule:
    Tuesday-Saturday valid dates: use the forecast issued one day earlier.
    Sunday valid dates: use Friday's two-day forecast.
    Monday valid dates: use Friday's three-day forecast.
If the expected issuance is missing, the script transparently falls back to
the latest available forecast issued before the valid date and labels it.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_URL = "https://www.airnowapi.org/aq/forecast/zipCode/"
TARGET_YEAR = 2023
TARGET_AREA_ALIASES = {"suburban dc", "suburban maryland"}
OZONE_ALIASES = {"o3", "ozone"}

# ZIPs inside/near the Maryland suburban forecast region. The script tests them
# and selects the first one whose AirNow response identifies the target area.
CANDIDATE_ZIPS = [
    "20910",  # Silver Spring
    "20912",  # Takoma Park
    "20814",  # Bethesda
    "20850",  # Rockville
    "20740",  # College Park
    "20770",  # Greenbelt
    "20782",  # Hyattsville
]
DISCOVERY_DATES = [date(2023, 6, 1), date(2023, 7, 1), date(2023, 8, 1)]

OUTPUT_DIR = Path("output")
OPERATIONAL_FILE = OUTPUT_DIR / "suburban_dc_ozone_forecasts_2023_operational.csv"
ALL_FILE = OUTPUT_DIR / "suburban_dc_ozone_forecasts_2023_all_leads.csv"
STRICT_DAY_AHEAD_FILE = OUTPUT_DIR / "suburban_dc_ozone_forecasts_2023_strict_day_ahead.csv"
SAME_DAY_FILE = OUTPUT_DIR / "suburban_dc_ozone_forecasts_2023_same_day.csv"
METADATA_FILE = OUTPUT_DIR / "suburban_dc_ozone_forecasts_2023_metadata.json"
DIAGNOSTIC_FILE = OUTPUT_DIR / "airnow_reporting_area_diagnostic.json"

FIELDNAMES = [
    "DateIssue",
    "DateForecast",
    "LeadDays",
    "ReportingArea",
    "StateCode",
    "Latitude",
    "Longitude",
    "ParameterName",
    "AQI",
    "CategoryNumber",
    "CategoryName",
    "ActionDay",
    "Discussion",
    "LookupZIP",
    "ExpectedIssueDate",
    "SelectionRule",
]


def normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def api_request(api_key: str, zip_code: str, issue_date: date) -> list[dict[str, Any]]:
    params = {
        "format": "application/json",
        "zipCode": zip_code,
        "date": issue_date.isoformat(),
        "distance": "25",
        "API_KEY": api_key,
    }
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    safe_url = BASE_URL + "?" + urllib.parse.urlencode(
        {key: value for key, value in params.items() if key != "API_KEY"}
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Suburban-DC-2023-forecast-research/1.0",
            "Accept": "application/json",
        },
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8-sig")
            parsed = json.loads(body)
            if isinstance(parsed, list):
                return [row for row in parsed if isinstance(row, dict)]
            raise RuntimeError(
                f"Unexpected AirNow response type for {safe_url}: "
                f"{type(parsed).__name__}"
            )
        except urllib.error.HTTPError as exc:
            # Retry temporary errors and rate limiting; never print the API key.
            if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(5 * attempt)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"AirNow HTTP {exc.code} for {safe_url}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"AirNow request failed for {safe_url}: {exc}") from exc

    raise RuntimeError(f"AirNow request failed for {safe_url}")


def extract_category(row: dict[str, Any]) -> tuple[Any, Any]:
    category = row.get("Category")
    if isinstance(category, dict):
        return (
            category.get("Number", row.get("CategoryNumber", "")),
            category.get("Name", row.get("CategoryName", "")),
        )
    return row.get("CategoryNumber", ""), row.get("CategoryName", "")


def is_target_area(row: dict[str, Any]) -> bool:
    return normalized(row.get("ReportingArea")) in TARGET_AREA_ALIASES


def is_ozone(row: dict[str, Any]) -> bool:
    return normalized(row.get("ParameterName")) in OZONE_ALIASES


def parse_iso_date(value: Any, field_name: str) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} value returned by AirNow: {text!r}") from exc


def discover_zip(api_key: str) -> tuple[str, dict[str, list[str]]]:
    seen: dict[str, set[str]] = {}
    for zip_code in CANDIDATE_ZIPS:
        seen[zip_code] = set()
        for issue_date in DISCOVERY_DATES:
            rows = api_request(api_key, zip_code, issue_date)
            seen[zip_code].update(
                str(row.get("ReportingArea", "")).strip()
                for row in rows
                if row.get("ReportingArea")
            )
            if any(is_target_area(row) for row in rows):
                return zip_code, {
                    key: sorted(values) for key, values in seen.items()
                }

    serializable = {key: sorted(values) for key, values in seen.items()}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DIAGNOSTIC_FILE.write_text(
        json.dumps(serializable, indent=2), encoding="utf-8"
    )
    raise RuntimeError(
        "Could not identify the Suburban DC/Suburban Maryland reporting area "
        "from the candidate ZIP codes. See output/airnow_reporting_area_diagnostic.json."
    )


def transform_row(row: dict[str, Any], lookup_zip: str) -> dict[str, Any] | None:
    if not is_target_area(row) or not is_ozone(row):
        return None

    issue = parse_iso_date(row.get("DateIssue"), "DateIssue")
    forecast = parse_iso_date(row.get("DateForecast"), "DateForecast")
    if forecast.year != TARGET_YEAR:
        return None

    category_number, category_name = extract_category(row)
    return {
        "DateIssue": issue.isoformat(),
        "DateForecast": forecast.isoformat(),
        "LeadDays": (forecast - issue).days,
        "ReportingArea": str(row.get("ReportingArea", "")).strip(),
        "StateCode": str(row.get("StateCode", "")).strip(),
        "Latitude": row.get("Latitude", ""),
        "Longitude": row.get("Longitude", ""),
        "ParameterName": str(row.get("ParameterName", "")).strip(),
        "AQI": row.get("AQI", ""),
        "CategoryNumber": category_number,
        "CategoryName": category_name,
        "ActionDay": row.get("ActionDay", ""),
        "Discussion": str(row.get("Discussion", "") or "").strip(),
        "LookupZIP": lookup_zip,
        "ExpectedIssueDate": "",
        "SelectionRule": "",
    }


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["DateIssue"],
            row["DateForecast"],
            normalized(row["ReportingArea"]),
            normalized(row["ParameterName"]),
        )
        by_key[key] = row

    return sorted(
        by_key.values(),
        key=lambda row: (
            row["DateForecast"],
            row["DateIssue"],
            row["LeadDays"],
        ),
    )


def expected_issue_date_for_operational_comparison(forecast_date: date) -> date:
    """Return the scheduled issue date for the official forecast to compare.

    Python weekday numbering: Monday=0, ..., Sunday=6.
    Sunday forecasts use Friday (2-day lead).
    Monday forecasts use Friday (3-day lead).
    All other valid dates use the preceding calendar day.
    """
    if forecast_date.weekday() == 6:  # Sunday
        return forecast_date - timedelta(days=2)
    if forecast_date.weekday() == 0:  # Monday
        return forecast_date - timedelta(days=3)
    return forecast_date - timedelta(days=1)


def select_operational_forecasts(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Select one official forecast per valid date using the operational schedule.

    Exact scheduled issue dates are preferred. If an expected issuance is absent,
    select the latest available issue strictly before the valid date and label it
    as a fallback. Same-day forecasts are never selected.
    """
    by_valid_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_valid_date.setdefault(row["DateForecast"], []).append(row)

    selected: list[dict[str, Any]] = []
    missing_dates: list[str] = []

    for valid_text in sorted(by_valid_date):
        valid_date = parse_iso_date(valid_text, "DateForecast")
        expected_issue = expected_issue_date_for_operational_comparison(valid_date)
        candidates = [
            row for row in by_valid_date[valid_text]
            if parse_iso_date(row["DateIssue"], "DateIssue") < valid_date
        ]

        exact = [
            row for row in candidates
            if parse_iso_date(row["DateIssue"], "DateIssue") == expected_issue
        ]

        chosen: dict[str, Any] | None = None
        rule = ""
        if exact:
            chosen = max(exact, key=lambda row: row["DateIssue"])
            if valid_date.weekday() == 6:
                rule = "Friday issuance for Sunday valid date"
            elif valid_date.weekday() == 0:
                rule = "Friday issuance for Monday valid date"
            else:
                rule = "Previous-day issuance"
        elif candidates:
            chosen = max(
                candidates,
                key=lambda row: parse_iso_date(row["DateIssue"], "DateIssue"),
            )
            rule = "Fallback: latest available prior issuance"
        else:
            missing_dates.append(valid_text)
            continue

        chosen_copy = dict(chosen)
        chosen_copy["ExpectedIssueDate"] = expected_issue.isoformat()
        chosen_copy["SelectionRule"] = rule
        selected.append(chosen_copy)

    return selected, missing_dates


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    api_key = os.environ.get("AIRNOW_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: AIRNOW_API_KEY is not set. Add it as a GitHub Actions "
            "repository secret or export it locally.",
            file=sys.stderr,
        )
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    lookup_zip, discovery_seen = discover_zip(api_key)
    print(f"Using ZIP {lookup_zip} to retrieve the target reporting area.")

    # Include the final six issue dates of 2022 because AirNow forecasts can
    # cover up to six days; retain only forecasts valid in calendar year 2023.
    issue_start = date(2022, 12, 26)
    issue_end = date(2023, 12, 31)

    extracted: list[dict[str, Any]] = []
    all_seen_areas: set[str] = set()
    request_count = 0

    for issue_date in daterange(issue_start, issue_end):
        rows = api_request(api_key, lookup_zip, issue_date)
        request_count += 1
        all_seen_areas.update(
            str(row.get("ReportingArea", "")).strip()
            for row in rows
            if row.get("ReportingArea")
        )
        for row in rows:
            transformed = transform_row(row, lookup_zip)
            if transformed is not None:
                extracted.append(transformed)

        if request_count % 50 == 0:
            print(f"Completed {request_count} historical-date requests.")

    all_rows = deduplicate(extracted)
    operational_rows, operational_missing_dates = select_operational_forecasts(all_rows)
    strict_day_ahead_rows = [row for row in all_rows if row["LeadDays"] == 1]
    same_day_rows = [row for row in all_rows if row["LeadDays"] == 0]

    write_csv(OPERATIONAL_FILE, operational_rows)
    write_csv(ALL_FILE, all_rows)
    write_csv(STRICT_DAY_AHEAD_FILE, strict_day_ahead_rows)
    write_csv(SAME_DAY_FILE, same_day_rows)

    metadata = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": "U.S. EPA AirNow API historical forecast web service",
        "source_documentation": "https://docs.airnowapi.org/webservices",
        "target_valid_year": TARGET_YEAR,
        "target_reporting_area_aliases": sorted(TARGET_AREA_ALIASES),
        "target_pollutant_aliases": sorted(OZONE_ALIASES),
        "lookup_zip": lookup_zip,
        "issue_date_start": issue_start.isoformat(),
        "issue_date_end": issue_end.isoformat(),
        "request_count_after_discovery": request_count,
        "row_counts": {
            "operational": len(operational_rows),
            "all_leads": len(all_rows),
            "same_day": len(same_day_rows),
            "strict_day_ahead": len(strict_day_ahead_rows),
        },
        "operational_selection": {
            "Tuesday_through_Saturday": "previous-calendar-day issuance",
            "Sunday": "Friday issuance (LeadDays=2)",
            "Monday": "Friday issuance (LeadDays=3)",
            "fallback": "latest available prior issuance if scheduled issue is absent",
            "missing_valid_dates": operational_missing_dates,
        },
        "lead_days_present": sorted(
            {int(row["LeadDays"]) for row in all_rows}
        ),
        "reporting_areas_seen": sorted(all_seen_areas),
        "discovery_reporting_areas_seen": discovery_seen,
        "notes": [
            "Use the operational CSV for the requested comparison.",
            "The operational CSV excludes same-day forecasts.",
            "Sunday uses Friday's 2-day forecast and Monday uses Friday's 3-day forecast.",
            "ExpectedIssueDate and SelectionRule make every selection auditable.",
            "Forecasts are reporting-area AQI forecasts, not monitor-level concentration forecasts.",
            "AQI may be -1 when the agency supplied only an AQI category.",
            "AirNow data are preliminary and may be revised.",
        ],
    }
    METADATA_FILE.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    if not all_rows:
        print(
            "ERROR: AirNow returned no matching 2023 ozone forecast rows. "
            f"Inspect {METADATA_FILE}.",
            file=sys.stderr,
        )
        return 3

    print(f"Wrote {len(operational_rows)} operational rows to {OPERATIONAL_FILE}")
    print(f"Wrote {len(all_rows)} all-lead rows to {ALL_FILE}")
    print(
        f"Wrote {len(strict_day_ahead_rows)} strict day-ahead rows "
        f"to {STRICT_DAY_AHEAD_FILE}"
    )
    print(f"Wrote {len(same_day_rows)} same-day rows to {SAME_DAY_FILE}")
    if operational_missing_dates:
        print(
            "WARNING: No prior official forecast was found for "
            f"{len(operational_missing_dates)} valid dates. See metadata."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
