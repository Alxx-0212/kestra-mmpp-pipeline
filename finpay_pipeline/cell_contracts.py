"""Versioned identifiers and source contracts for the separate cell path.

The observed roster is promoted for cell-format acquisition and checked against
the live portal. This does not commission database, workbook, editor, or credential
targets; the new report path seeds its running balance internally at zero.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from dataclasses import asdict, dataclass
from datetime import date, datetime

CELL_SCHEMA_VERSION = 1
MAX_EXACT_RUPIAH = 2 ** 53 - 1
MAX_CELL_ARTIFACT_BYTES = 32 * 1024 * 1024


def read_bounded_bytes(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("input file exceeds configured size limit")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input file grew beyond configured size limit")
    return data


def sha256_file_bounded(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("artifact exceeds configured size limit")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(min(1024 * 1024, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("artifact grew beyond configured size limit")
            digest.update(chunk)
    return digest.hexdigest()
OBSERVED_CELL_ROSTER = {
    "422040_B": ("422041", "422042", "422043"),
    "422140_B": ("422141", "422142"),
    "412110_B": ("412111", "412112"),
    "422010_B": ("422011", "422012", "422013"),
    "422090_B": ("422091", "422092"),
    "422100_B": ("422101", "422102"),
    "422180_B": ("422181", "422182"),
    "422220_B": ("422221", "422222"),
}


def fingerprint(value) -> str:
    """Hash canonical JSON without execution timestamps or incidental row order."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def parse_cell_amount(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"invalid required {field} integer")
    text = str(value).strip()
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)", text):
        raise ValueError(f"invalid required {field} amount grouping")
    amount = int(text.replace(",", ""))
    if abs(amount) > MAX_EXACT_RUPIAH:
        raise ValueError("source amount exceeds exact spreadsheet integer precision")
    return amount


def business_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("business date must be YYYY-MM-DD")
    return datetime.strptime(value, "%Y-%m-%d").date()


@dataclass(frozen=True)
class CellIdentity:
    account: str
    cluster_id: str
    cell_code: str
    parentcode: str

    def __post_init__(self):
        if not re.fullmatch(r"\d{6}_B", self.account):
            raise ValueError("account must be a six-digit _B login")
        if self.cluster_id != self.account[:-2]:
            raise ValueError("cluster does not match account")
        if not re.fullmatch(r"\d{6}", self.cell_code):
            raise ValueError("cell code must contain six digits")
        if self.parentcode != self.cluster_id + self.cell_code:
            raise ValueError("parentcode must concatenate cluster and cell")

    def to_dict(self) -> dict:
        return asdict(self)


def configured_cells(roster: dict[str, tuple[str, ...] | list[str]]) -> tuple[CellIdentity, ...]:
    if not roster:
        raise ValueError("an explicit non-empty cell roster is required")
    cells = []
    for account, codes in sorted(roster.items()):
        if not isinstance(codes, (tuple, list)) or not 1 <= len(codes) <= 3:
            raise ValueError("each account needs one to three cells")
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate cell in roster")
        for code in sorted(codes):
            cells.append(CellIdentity(account, account[:-2], code, account[:-2] + code))
    return tuple(cells)


def roster_fingerprint(cells: tuple[CellIdentity, ...]) -> str:
    return fingerprint([cell.to_dict() for cell in cells])


def validate_source_entry(entry: dict, identity: CellIdentity, report_date: str) -> None:
    business_date(report_date)
    for key, expected in {**identity.to_dict(), "report_date": report_date}.items():
        if entry.get(key) != expected:
            raise ValueError(f"source manifest {key} mismatch")
    rows = entry.get("validated_rows")
    if type(rows) is not int or rows < 0 or type(entry.get("footer_total")) is not int or entry.get("footer_total") != rows:
        raise ValueError("source manifest counts must agree")
    if entry.get("status") != ("empty" if rows == 0 else "validated"):
        raise ValueError("source manifest status/count mismatch")
    if rows == 0 and entry.get("artifact") is not None:
        raise ValueError("empty source cannot claim an XLSX artifact")
    if rows and not isinstance(entry.get("artifact"), str):
        raise ValueError("validated source requires an artifact")
    if not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("source_sha256", ""))):
        raise ValueError("source manifest requires a SHA-256")


def validate_complete_manifest(manifest: dict, cells: tuple[CellIdentity, ...]) -> None:
    if manifest.get("schema_version") != CELL_SCHEMA_VERSION:
        raise ValueError("unsupported cell manifest schema")
    if manifest.get("roster_fingerprint") != roster_fingerprint(cells):
        raise ValueError("cell roster fingerprint mismatch")
    entries = manifest.get("cells")
    if not isinstance(entries, list) or len(entries) != len(cells):
        raise ValueError("incomplete cell manifest")
    by_key = {}
    for entry in entries:
        key = (entry.get("account"), entry.get("cell_code"))
        if key in by_key:
            raise ValueError("duplicate cell manifest entry")
        by_key[key] = entry
    for cell in cells:
        entry = by_key.get((cell.account, cell.cell_code))
        if entry is None:
            raise ValueError("missing cell manifest entry")
        validate_source_entry(entry, cell, manifest.get("report_date"))
