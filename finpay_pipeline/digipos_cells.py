"""Separate, manifest-scoped DigiPOS cell acquisition API (legacy stays intact)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl

if __package__:
    from .cell_contracts import (
        CELL_SCHEMA_VERSION, MAX_CELL_ARTIFACT_BYTES, CellIdentity, business_date, configured_cells,
        fingerprint, parse_cell_amount, read_bounded_bytes, roster_fingerprint,
        sha256_file_bounded, validate_complete_manifest, validate_source_entry,
    )
    from .digipos_scraper import (
        BASE_URL, ENDPOINT_PATH, MAX_TABLE_ROWS,
        _read_xlsx_rows, _replace_datatables_length, _set_search_filters,
        _write_xhr_export, parse_datatable_total, validate_export_rows,
    )
else:
    from cell_contracts import (
        CELL_SCHEMA_VERSION, MAX_CELL_ARTIFACT_BYTES, CellIdentity, business_date, configured_cells,
        fingerprint, parse_cell_amount, read_bounded_bytes, roster_fingerprint,
        sha256_file_bounded, validate_complete_manifest, validate_source_entry,
    )
    from digipos_scraper import (
        BASE_URL, ENDPOINT_PATH, MAX_TABLE_ROWS,
        _read_xlsx_rows, _replace_datatables_length, _set_search_filters,
        _write_xhr_export, parse_datatable_total, validate_export_rows,
    )

ENDPOINT = re.compile(r"/deposit/monitoring-riwayat-data(?:\?|$)")


def cell_row_to_cells(row: dict, index: int) -> list:
    """Keep source strings verbatim; normalize only monetary/derived RS fields."""
    identifier = row.get("wstransferid")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("transaction identifiers must be nonblank exact source text")
    text = {}
    for key in ("tanggal", "transaction_type", "wstransfertype", "wstransferdesc"):
        value = row.get(key)
        if not isinstance(value, str):
            raise ValueError("required source text field is not a string")
        text[key] = value
    phone = str(row.get("sumber1") or "").strip()
    if phone.startswith("49"):
        phone = phone[2:]
    nomor_rs = (phone or "-") if text["transaction_type"].strip() == "Credit" else "-"
    return [
        index, text["tanggal"], identifier,
        parse_cell_amount(row.get("init_amount"), "init_amount"),
        parse_cell_amount(row.get("amount_in"), "amount_in"),
        parse_cell_amount(row.get("amount_out"), "amount_out"),
        parse_cell_amount(row.get("last_amount"), "last_amount"),
        text["transaction_type"], text["wstransfertype"], nomor_rs, text["wstransferdesc"],
    ]


def _request_fields(post_data: str | None) -> dict:
    pairs = parse_qsl(post_data or "", keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate DigiPOS request parameters")
    return dict(pairs)


def _count(value) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError("invalid DigiPOS count")
    return int(value)


def validate_cell_payload(payload: dict, request: dict, footer: int, full: bool) -> list:
    """Require explicit success or a correlated complete DataTables envelope."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("DigiPOS response needs an explicit data array")
    if type(footer) is not int or not 0 <= footer <= MAX_TABLE_ROWS:
        raise ValueError("invalid or excessive DigiPOS footer count")
    if "statusCode" in payload:
        if str(payload["statusCode"]) != "00":
            raise ValueError("DigiPOS business response was not successful")
    elif not all(key in payload for key in ("draw", "recordsFiltered", "recordsTotal")):
        raise ValueError("DigiPOS response has no recognized success evidence")
    if "draw" in payload and _count(payload["draw"]) != _count(request.get("draw")):
        raise ValueError("DigiPOS response draw mismatch")
    if "recordsFiltered" in payload and _count(payload["recordsFiltered"]) != footer:
        raise ValueError("DigiPOS response/footer count mismatch")
    if "recordsTotal" in payload and _count(payload["recordsTotal"]) < footer:
        raise ValueError("DigiPOS recordsTotal is less than filtered total")
    data = payload["data"]
    if len(data) > footer or (full and len(data) != footer):
        raise ValueError("DigiPOS response row count mismatch")
    if any(not isinstance(row, dict) for row in data):
        raise ValueError("DigiPOS data rows must be objects")
    return data


def _exchange(page, identity: CellIdentity, report_date: str, *, full_length=None):
    """Tie the response to the intercepted request, not the first pending XHR."""
    captured = []
    errors = []
    expected = {
        "adNo": identity.account,
        "parentcode": identity.parentcode,
        "startDate": report_date + " 00:00:00",
        "endDate": report_date + " 23:59:59",
    }

    def route_request(route):
        request = route.request
        try:
            fields = _request_fields(request.post_data)
            if request.method != "POST" or any(fields.get(k) != v for k, v in expected.items()):
                raise ValueError("DigiPOS request account/cell/date mismatch")
            _count(fields.get("draw"))
            if fields.get("start") != "0":
                raise ValueError("DigiPOS request is not the first page")
            body = request.post_data
            if full_length is not None:
                body = _replace_datatables_length(body, full_length)
                fields = _request_fields(body)
            captured.append((request, fields))
            if full_length is None:
                route.continue_()
            else:
                headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
                headers["content-length"] = str(len(body.encode("utf-8")))
                route.continue_(post_data=body, headers=headers)
        except ValueError as exc:
            errors.append(str(exc))
            route.abort("failed")

    page.route(ENDPOINT, route_request)
    try:
        with page.expect_response(
            lambda response: ENDPOINT_PATH in response.url
            and any(response.request is request for request, _ in captured),
            timeout=180_000,
        ) as response_info:
            if full_length is None:
                page.click("#btn_search", timeout=30_000)
            else:
                page.evaluate(
                    "n => $('#dataInput').DataTable().page.len(n).page('first').draw()",
                    full_length,
                )
        response = response_info.value
        if errors or len(captured) != 1:
            raise ValueError(errors[0] if errors else "ambiguous DigiPOS requests")
        if response.status != 200:
            raise ValueError("DigiPOS XHR HTTP failure")
        return response.json(), captured[0][1]
    finally:
        page.unroute(ENDPOINT, route_request)


def _install_compatibility(page):
    # Pin only the observed jQuery dependency, never arbitrary Google requests.
    def jquery(route):
        response = route.fetch(url="https://cdn.jsdelivr.net/npm/jquery@3.6.4/dist/jquery.min.js")
        if response.status != 200:
            raise RuntimeError("jQuery compatibility mirror unavailable")
        route.fulfill(response=response)

    page.route(
        re.compile(r"^https://ajax\.googleapis\.com/ajax/libs/jquery/3\.6\.4/jquery\.min\.js$"),
        jquery,
    )
    page.route(
        re.compile(r"^https://fonts\.googleapis\.com/"),
        lambda route: route.fulfill(status=200, content_type="text/css", body=""),
    )


def _download_cell(
    browser, identity, report_date, password, directory,
    googleapis_compatibility, expected_parentcodes,
):
    context = browser.new_context(service_workers="block", accept_downloads=True)
    try:
        page = context.new_page()
        if googleapis_compatibility:
            _install_compatibility(page)
        page.goto(BASE_URL + "/login", wait_until="domcontentloaded", timeout=90_000)
        page.locator("#username").fill(identity.account)
        page.locator("#password").fill(password)
        page.evaluate("$('button:contains(\"Sign in\")').trigger('click')")
        page.wait_for_url("**/home", timeout=30_000)
        page.goto(BASE_URL + "/deposit/monitoring-riwayat", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof $ !== 'undefined' && $.fn.DataTable.isDataTable('#dataInput')"
        )
        options = page.evaluate(
            "() => Array.from(document.querySelector('#parentPhoneNo_pick').options).map(o => o.value)"
        )
        concrete = sorted(v for v in options if v != identity.cluster_id)
        if concrete != expected_parentcodes:
            raise ValueError("live cell roster differs from configured roster")
        _set_search_filters(page, business_date(report_date))
        if identity.parentcode not in options:
            raise ValueError("configured cell option missing")
        selected = page.evaluate(
            "v => { $('#parentPhoneNo_pick').val(v).trigger('change'); return $('#parentPhoneNo').val(); }",
            identity.parentcode,
        )
        if selected != identity.parentcode:
            raise ValueError("cell selection did not stick")
        payload, request = _exchange(page, identity, report_date)
        # Validate business success before waiting for an overlay caused by error.
        if "statusCode" in payload and str(payload["statusCode"]) != "00":
            raise ValueError("DigiPOS business response was not successful")
        page.wait_for_function(
            """() => !$('#loadingDialog').is(':visible') && !$('#modal-gagal').is(':visible')
                && /of\\s+[\\d,]+\\s+entries/.test($('#dataInput_info').text())""",
            timeout=180_000,
        )
        footer = parse_datatable_total(page.locator("#dataInput_info").inner_text())
        validate_cell_payload(payload, request, footer, full=False)
        result = {
            **identity.to_dict(), "report_date": report_date,
            "footer_total": footer, "validated_rows": footer,
            "discovered_parentcodes": concrete,
            "artifact": None, "source_sha256": None,
            "status": "empty" if footer == 0 else "validated",
        }
        if footer == 0:
            result["source_sha256"] = fingerprint({
                **identity.to_dict(), "report_date": report_date, "data": [],
            })
            return result
        payload, request = _exchange(page, identity, report_date, full_length=footer)
        raw_rows = validate_cell_payload(payload, request, footer, full=True)
        if any(not isinstance(row.get("wstransferid"), str) for row in raw_rows):
            raise ValueError("transaction identifiers must be exact source text")
        rows = [cell_row_to_cells(row, index) for index, row in enumerate(raw_rows, 1)]
        name = f"{identity.account}-{identity.cell_code}.xlsx"
        final = directory / name
        pending = directory / ("." + name)
        try:
            _write_xhr_export(pending, rows)
            digest = sha256_file_bounded(pending, MAX_CELL_ARTIFACT_BYTES)
            validate_export_rows(_read_xlsx_rows(pending), report_date, footer)
            os.replace(pending, final)
        finally:
            pending.unlink(missing_ok=True)
        result.update(artifact=name, source_sha256=digest)
        return result
    finally:
        context.close()


def _resume_entry(entry, identity, report_date, directory):
    validate_source_entry(entry, identity, report_date)
    if entry["status"] == "empty":
        if entry["source_sha256"] != fingerprint({
            **identity.to_dict(), "report_date": report_date, "data": [],
        }):
            raise ValueError("empty evidence checksum mismatch")
        return
    path = (directory / entry["artifact"]).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        raise ValueError("source artifact path outside acquisition directory")
    if sha256_file_bounded(path, MAX_CELL_ARTIFACT_BYTES) != entry["source_sha256"]:
        raise ValueError("source artifact checksum mismatch")
    validate_export_rows(_read_xlsx_rows(path), report_date, entry["validated_rows"])


def scrape_cell_roster(
    report_date: str, password: str, roster: dict, output_dir: str,
    *, max_attempts: int = 3, googleapis_compatibility: bool = False,
) -> dict:
    """Acquire an explicit roster serially; preserve validated local partial progress.

    Failure leaves a partial state file, not a success manifest. Recovery across
    failed Kestra tasks needs separately verified durable artifact storage.
    """
    cells = configured_cells(roster)
    business_date(report_date)
    if not password or type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("password and positive max_attempts are required")
    directory = Path(output_dir) / report_date
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "cell_acquisition_state.json"
    result = {
        "schema_version": CELL_SCHEMA_VERSION, "report_date": report_date,
        "roster_fingerprint": roster_fingerprint(cells), "cells": [],
    }
    if state_path.is_file():
        previous = json.loads(read_bounded_bytes(state_path, 2 * 1024 * 1024))
        if any(previous.get(key) != result[key] for key in result if key != "cells"):
            raise ValueError("partial acquisition identity mismatch")
        result["cells"] = previous["cells"]
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for identity in cells:
                existing = [item for item in result["cells"] if
                            (item["account"], item["cell_code"]) == (identity.account, identity.cell_code)]
                if existing:
                    if len(existing) != 1:
                        raise ValueError("duplicate partial acquisition entry")
                    try:
                        _resume_entry(existing[0], identity, report_date, directory)
                    except Exception:
                        # The cached evidence is unusable. Reacquire this cell,
                        # while retaining checksum-validated peer artifacts.
                        result["cells"].remove(existing[0])
                    else:
                        continue
                for attempt in range(1, max_attempts + 1):
                    try:
                        expected = sorted(cell.parentcode for cell in cells if cell.account == identity.account)
                        entry = _download_cell(
                            browser, identity, report_date, password, directory,
                            googleapis_compatibility, expected,
                        )
                        if entry["discovered_parentcodes"] != expected:
                            raise ValueError("live cell roster differs from configured roster")
                        validate_source_entry(entry, identity, report_date)
                        result["cells"].append(entry)
                        pending = state_path.with_suffix(".pending.json")
                        pending.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
                        os.replace(pending, state_path)
                        break
                    except Exception as exc:
                        if attempt == max_attempts:
                            raise RuntimeError(
                                f"cell {identity.account}/{identity.cell_code} acquisition failed: "
                                f"{type(exc).__name__}"
                            ) from None
        finally:
            browser.close()
    validate_complete_manifest(result, cells)
    return result
