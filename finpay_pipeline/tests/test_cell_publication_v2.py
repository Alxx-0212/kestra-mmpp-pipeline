"""Synthetic multi-tab publication protocol tests; no Google/DB client is used."""
import copy
import json
import unittest
from datetime import date
from unittest.mock import patch

from finpay_pipeline.cell_publication import CellWorksheetTarget
from finpay_pipeline.cell_publication_v2 import publish_cell_cluster_v2, validate_output_targets


class MultiTabStore:
    def __init__(self):
        self.cluster_id = "412110"
        self.target_id = "dummy-v2-workbook"
        self.alive = True
        self.facts = {}
        self.publications = {}
        self.operation = "IDLE"
        self.events = []
        self.dirty = {}
        self.fail_audit = False
        self.adapters = {}

    def assert_alive(self):
        if not self.alive:
            raise RuntimeError("test lock lost")

    def latest_publication(self, *, sheet_only=False):
        available = {epoch: item for epoch, item in self.publications.items()
                     if not sheet_only or item["status"] != "DB_ONLY"}
        if not available:
            return None
        epoch = max(available)
        item = available[epoch]
        return {"epoch": epoch, **copy.deepcopy(item)}

    def latest_report_date(self):
        return max((day for day, _ in self.facts), default=None)

    def current_results(self, start, end):
        rows = []
        for (day, code), item in sorted(self.facts.items()):
            if start <= day <= end:
                rows.append({"report_date": date.fromisoformat(day), "account": item["identity"]["account"],
                    "cluster_id": item["identity"]["cluster_id"], "cell_code": code,
                    "generation_id": item["generation_id"], "calculation_id": item["calculation_id"],
                    "config_id": item["config_id"], "result": item["calculation"]})
        return rows

    def dirty_dates(self, *, reset_only=False):
        return [day for day, reset in self.dirty.items() if reset or not reset_only]

    def begin_target_operation(self, operation):
        if self.operation != "IDLE":
            raise RuntimeError("test target is not idle")
        self.operation = "IN_FLIGHT"
        self.events.append("operation_begin")

    def finish_target_operation(self, operation, unknown_error=None, known_error=None):
        self.operation = "UNKNOWN" if unknown_error else "IDLE"
        self.operation_error = unknown_error or known_error

    def commit_and_stage(self, batch, expected_cells, render, *, write_gsheet, additional_batches=()):
        previous = copy.deepcopy(self.facts)
        previous_dirty = self.dirty.copy()
        try:
            for group in [*additional_batches, batch]:
                for item in group:
                    key = (item["report_date"], item["identity"]["cell_code"])
                    old = self.facts.get(key)
                    reset = old is None or old["generation_id"] != item["generation_id"]
                    if old is None or old["calculation_id"] != item["calculation_id"]:
                        self.dirty[key[0]] = self.dirty.get(key[0], False) or reset
                    self.facts[key] = item
            rendered = render(self)
            digest = __import__("finpay_pipeline.cell_contracts", fromlist=["fingerprint"]).fingerprint({
                "cluster_id": self.cluster_id, "target_id": self.target_id,
                "range_start": rendered["start"], "range_end": rendered["end"],
                "snapshot": rendered["snapshot"],
            })
            prior = self.latest_publication()
            if prior and prior["fingerprint"] == digest and not rendered.get("refresh_audit_required"):
                return {"epoch": prior["epoch"], "fingerprint": digest, "status": prior["status"]}
            epoch = max(self.publications, default=0) + 1
            self.publications[epoch] = {"snapshot": rendered["snapshot"], "status": "PENDING",
                                        "fingerprint": digest}
            self.events.append("db_commit")
            return {"epoch": epoch, "fingerprint": digest, "status": "PENDING"}
        except Exception:
            self.facts, self.dirty = previous, previous_dirty
            raise

    def publication_snapshot(self, epoch):
        return copy.deepcopy(self.publications[epoch])

    def record_manual_reset(self, *args):
        self.events.append("manual_audit")
        if self.fail_audit:
            raise RuntimeError("audit failed")

    def begin_sheet_write(self, epoch):
        self.publications[epoch]["status"] = "IN_FLIGHT"
        self.events.append("sheets_begin")

    def confirm_chunk(self, epoch, label):
        self.events.append(label)

    def mark_unknown(self, epoch, error):
        self.publications[epoch]["status"] = "UNKNOWN"

    def mark_pending(self, epoch, error):
        self.publications[epoch].update(status="PENDING", error_class=error)

    def complete(self, epoch, verified_fingerprint):
        assert self.publications[epoch]["fingerprint"] == verified_fingerprint
        self.publications[epoch]["status"] = "COMPLETE"
        self.events.append("db_complete")
        self.dirty.clear()


class MultiTabAdapter:
    def __init__(self, role, sheet_id, title, store, fail_write=False):
        self.role, self.sheet_id, self.title, self.store = role, sheet_id, title, store
        self.spreadsheet_id = store.target_id
        self.values = []
        self.metadata_entries, self.protections = [], []
        self.next_id = sheet_id * 100
        self.fail_write = fail_write
        self.events = []
        self.styles, self.widths, self.heights = {}, {}, {}
        self.merges, self.banding = [], []
        self.grid = {"rowCount": 100, "columnCount": 100}

    @property
    def worksheet_id(self):
        return self.sheet_id

    def metadata(self):
        return {"properties": {"sheetId": self.sheet_id, "title": self.title,
                               "gridProperties": copy.deepcopy(self.grid)},
                "merges": copy.deepcopy(self.merges), "bandedRanges": copy.deepcopy(self.banding),
                "developerMetadata": copy.deepcopy(self.metadata_entries),
                "protectedRanges": copy.deepcopy(self.protections), "implicitOwnerEmails": []}

    def read(self, rows, columns):
        return [row[:columns] for row in self.values[:rows]]

    def native_format(self, row, column):
        value = copy.deepcopy(self.styles.get((row, column), {}))
        for area in self.merges:
            first, last = area.get("startRowIndex", 0), area["endRowIndex"]
            left, right = area.get("startColumnIndex", 0), area["endColumnIndex"]
            if first <= row < last and left <= column < right:
                if (row, column) != (first, left):
                    return {}
                edges = value.setdefault("borders", {})
                for side, r, c in (("top", first, left), ("bottom", last - 1, left),
                                   ("left", first, left), ("right", first, right - 1)):
                    edge = self.styles.get((r, c), {}).get("borders", {}).get(side)
                    if edge is None:
                        edges.pop(side, None)
                    else:
                        edges[side] = copy.deepcopy(edge)
                return value
        return value

    def read_style(self, rows, columns):
        for start in range(0, rows, 100):
            stop = min(start + 100, rows)
            yield {"start": start, "grid": copy.deepcopy(self.grid), "merges": copy.deepcopy(self.merges),
                "banding": copy.deepcopy(self.banding), "default": {},
                "widths": [self.widths.get(col, 100) for col in range(columns)],
                "heights": [self.heights.get(row, 21) for row in range(start, stop)],
                "formats": [[self.native_format(row, col) for col in range(columns)]
                            for row in range(start, stop)],
                "entered": [[copy.deepcopy(self.styles.get((row, col), {})) for col in range(columns)]
                            for row in range(start, stop)]}

    def apply_style(self, area, value, fields):
        for row in range(area["startRowIndex"], area["endRowIndex"]):
            for col in range(area["startColumnIndex"], area["endColumnIndex"]):
                key = (row, col)
                if fields == "userEnteredFormat":
                    self.styles[key] = copy.deepcopy(value)
                    continue
                paths = (fields[len("userEnteredFormat("):-1].split(",") if fields.startswith("userEnteredFormat(")
                         else [part.removeprefix("userEnteredFormat.") for part in fields.split(",")])
                for path in paths:
                    source, target = value, self.styles.setdefault(key, {})
                    parts = path.split(".")
                    for name in parts[:-1]:
                        source = source.get(name, {})
                        target = target.setdefault(name, {})
                    if parts[-1] in source:
                        target[parts[-1]] = copy.deepcopy(source[parts[-1]])
                    else:
                        target.pop(parts[-1], None)

    def batch_update(self, requests):
        replies = []
        for request in requests:
            payload = next(iter(request.values()))
            sheet_id = payload.get("range", {}).get("sheetId") or payload.get("properties", {}).get("sheetId")
            if "autoResizeDimensions" in request:
                sheet_id = payload["dimensions"]["sheetId"]
            elif "deleteBanding" in request:
                sheet_id = next(adapter.sheet_id for adapter in self.store.adapters.values()
                    if any(item["bandedRangeId"] == payload["bandedRangeId"] for item in adapter.banding))
            elif "addProtectedRange" in request:
                sheet_id = payload["protectedRange"]["range"]["sheetId"]
            elif "createDeveloperMetadata" in request:
                sheet_id = payload["developerMetadata"]["location"]["sheetId"]
            elif "updateProtectedRange" in request or "deleteProtectedRange" in request:
                protection_id = payload.get("protectedRange", payload)["protectedRangeId"]
                sheet_id = next(adapter.sheet_id for adapter in self.store.adapters.values()
                    if any(item["protectedRangeId"] == protection_id for item in adapter.protections))
            elif "updateDeveloperMetadata" in request:
                metadata_id = payload["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
                sheet_id = next(adapter.sheet_id for adapter in self.store.adapters.values()
                    if any(item["metadataId"] == metadata_id for item in adapter.metadata_entries))
            if sheet_id is not None and sheet_id != self.sheet_id:
                peer = next(adapter for adapter in self.store.adapters.values() if adapter.sheet_id == sheet_id)
                replies.extend(peer.batch_update([request])["replies"])
                continue
            if "updateSheetProperties" in request:
                self.grid.update(payload["properties"].get("gridProperties", {}))
                replies.append({})
                continue
            elif "updateDimensionProperties" in request:
                area = payload["range"]
                target = self.widths if area["dimension"] == "COLUMNS" else self.heights
                for index in range(area["startIndex"], area["endIndex"]):
                    target[index] = payload["properties"]["pixelSize"]
                replies.append({})
                continue
            elif "repeatCell" in request:
                self.events.append("format")
                self.apply_style(payload["range"], payload["cell"].get("userEnteredFormat", {}), payload["fields"])
                replies.append({})
                continue
            elif "mergeCells" in request:
                self.merges.append(copy.deepcopy(payload["range"]))
                replies.append({})
                continue
            elif "unmergeCells" in request:
                self.merges = [item for item in self.merges if item != payload["range"]]
                replies.append({})
                continue
            elif "deleteBanding" in request:
                self.banding = [item for item in self.banding if item["bandedRangeId"] != payload["bandedRangeId"]]
                replies.append({})
                continue
            elif "updateBorders" in request:
                area = payload["range"]
                for row in range(area["startRowIndex"], area["endRowIndex"]):
                    for col in range(area["startColumnIndex"], area["endColumnIndex"]):
                        borders = self.styles.setdefault((row, col), {}).setdefault("borders", {})
                        for side, active in (("top", row == area["startRowIndex"]),
                            ("bottom", row == area["endRowIndex"] - 1), ("left", col == area["startColumnIndex"]),
                            ("right", col == area["endColumnIndex"] - 1)):
                            if active and side in payload:
                                borders[side] = copy.deepcopy(payload[side])
                                if payload[side].get("style") == "NONE":
                                    dr, dc, opposite = {"top": (-1, 0, "bottom"), "bottom": (1, 0, "top"),
                                        "left": (0, -1, "right"), "right": (0, 1, "left")}[side]
                                    neighbor = self.styles.get((row + dr, col + dc), {})
                                    neighbor.get("borders", {}).pop(opposite, None)
                        if "innerHorizontal" in payload:
                            if row > area["startRowIndex"]:
                                borders["top"] = copy.deepcopy(payload["innerHorizontal"])
                            if row < area["endRowIndex"] - 1:
                                borders["bottom"] = copy.deepcopy(payload["innerHorizontal"])
                        if "innerVertical" in payload:
                            if col > area["startColumnIndex"]:
                                borders["left"] = copy.deepcopy(payload["innerVertical"])
                            if col < area["endColumnIndex"] - 1:
                                borders["right"] = copy.deepcopy(payload["innerVertical"])
                replies.append({})
                continue
            if "addProtectedRange" in request:
                item = copy.deepcopy(request["addProtectedRange"]["protectedRange"])
                item["protectedRangeId"] = self.next_id
                self.next_id += 1
                self.protections.append(item)
                replies.append({"addProtectedRange": {"protectedRange": item}})
            elif "updateProtectedRange" in request:
                update = request["updateProtectedRange"]
                item = next(p for p in self.protections
                            if p["protectedRangeId"] == update["protectedRange"]["protectedRangeId"])
                for field in update["fields"].split(","):
                    item[field] = copy.deepcopy(update["protectedRange"][field])
                replies.append({})
            elif "deleteProtectedRange" in request:
                self.protections = [item for item in self.protections
                    if item["protectedRangeId"] != payload["protectedRangeId"]]
                replies.append({})
            elif "createDeveloperMetadata" in request:
                item = copy.deepcopy(request["createDeveloperMetadata"]["developerMetadata"])
                item["metadataId"] = self.next_id
                self.next_id += 1
                self.metadata_entries.append(item)
                replies.append({})
            elif "updateDeveloperMetadata" in request:
                item = self.metadata_entries[0]
                item["metadataValue"] = request["updateDeveloperMetadata"]["developerMetadata"]["metadataValue"]
                replies.append({})
            elif "updateCells" in request:
                update = request["updateCells"]
                if update["fields"] == "note":
                    replies.append({})
                    continue
                if self.fail_write:
                    raise TimeoutError("fake tab write failed")
                grid = update["range"]
                self.store.events.append("sheets:" + self.role)
                while len(self.values) < grid["endRowIndex"]:
                    self.values.append([""] * grid["endColumnIndex"])
                for r in range(grid["startRowIndex"], grid["endRowIndex"]):
                    local = r - grid["startRowIndex"]
                    source = update["rows"][local]["values"] if local < len(update["rows"]) else []
                    while len(self.values[r]) < grid["endColumnIndex"]:
                        self.values[r].append("")
                    for c in range(grid["startColumnIndex"], grid["endColumnIndex"]):
                        index = c - grid["startColumnIndex"]
                        entry = source[index] if index < len(source) else {}
                        value = entry.get("userEnteredValue", {})
                        self.values[r][c] = next(iter(value.values())) if value else ""
                        if "userEnteredFormat" in update["fields"]:
                            self.styles[(r, c)] = copy.deepcopy(entry.get("userEnteredFormat", {}))
                replies.append({})
            else:
                replies.append({})
        return {"replies": replies}


class CellPublicationV2Tests(unittest.TestCase):
    def test_alias_targets_publish_with_the_same_id_and_role_guards(self):
        from dataclasses import replace
        from finpay_pipeline.cell_publication_v2 import cell_worksheet_title
        self.roles = {role: replace(item, title=cell_worksheet_title(self.config.account, role, "cluster_alias"),
            naming_scheme="cluster_alias") for role, item in self.roles.items()}
        for role, item in self.roles.items():
            self.adapters[role].title = item.title
        self.assertEqual(self.publish()["status"], "COMPLETE")
        self.assertEqual(self.adapters["summary"].title, "PKY")

    def setUp(self):
        from finpay_pipeline.tests.test_cell_calculation import CellCalculationTests
        fixture = CellCalculationTests()
        fixture.setUp()
        self.fixture, self.config = fixture, fixture.config
        self.batch = [fixture.prepare(code, [fixture.row()]) for code in self.config.cell_codes]
        self.store = MultiTabStore()
        self.roles = {role: CellWorksheetTarget(role, self.store.target_id, 30 + idx,
            f"412110 {role.title()}", "writer@example.test", ("finance@example.test",))
            for idx, role in enumerate(("summary", "invoice", "unusual", "reversal"))}
        self.adapters = {role: MultiTabAdapter(role, target.worksheet_id, target.title, self.store)
                         for role, target in self.roles.items()}
        self.store.adapters = self.adapters

    def publish(self, **kwargs):
        return publish_cell_cluster_v2(self.store, self.store.target_id, self.roles, self.adapters,
            self.batch, self.config, changed_dates=[self.batch[0]["report_date"]], **kwargs)

    def test_four_output_tabs_share_one_complete_epoch_and_cell_data(self):
        result = self.publish()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(self.store.publications[1]["status"], "COMPLETE")
        self.assertEqual(self.store.operation, "IDLE")
        for role, adapter in self.adapters.items():
            marker = json.loads(adapter.metadata_entries[0]["metadataValue"])
            self.assertEqual(marker, {"status": "COMPLETE", "epoch": 1, "fingerprint": result["fingerprint"]})
            self.assertTrue(adapter.values)
        self.assertEqual(self.adapters["unusual"].values[0][1], "CELL")
        self.assertEqual(self.adapters["reversal"].values[0][1], "CELL")
        self.assertNotIn("LINKAJA", str(self.adapters["invoice"].values))

    def test_missing_or_duplicate_role_target_fails_before_db_commit(self):
        bad = dict(self.roles)
        del bad["invoice"]
        with self.assertRaisesRegex(ValueError, "four output roles"):
            publish_cell_cluster_v2(self.store, self.store.target_id, bad, self.adapters, self.batch,
                                    self.config, changed_dates=["2026-10-13"])
        self.assertFalse(self.store.facts)
        dup = dict(self.roles)
        dup["invoice"] = CellWorksheetTarget("invoice", self.store.target_id, 30,
                                               "412110 Invoice", "writer@example.test", ("finance@example.test",))
        with self.assertRaisesRegex(ValueError, "unique"):
            publish_cell_cluster_v2(self.store, self.store.target_id, dup, self.adapters, self.batch,
                                    self.config, changed_dates=["2026-10-13"])
        self.assertFalse(self.store.facts)

    def test_partial_tab_failure_leaves_all_epoch_markers_pending_or_unknown(self):
        self.adapters["invoice"].fail_write = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.store.publications[1]["status"], "UNKNOWN")
        self.assertEqual(self.store.operation, "UNKNOWN")
        for adapter in self.adapters.values():
            self.assertEqual(json.loads(adapter.metadata_entries[0]["metadataValue"])["status"], "PENDING")

    def test_rerun_verifies_same_epoch_without_rewriting_values(self):
        first = self.publish()
        writes = self.store.events.count("sheets:summary")
        second = self.publish()
        self.assertEqual(first, second)
        self.assertEqual(self.store.events.count("sheets:summary"), writes)
        self.assertEqual(self.store.operation, "IDLE")

    def test_history_publish_then_rerun_fingerprints_actual_full_written_range(self):
        for day in ("2026-10-13", "2026-10-14", "2026-10-15"):
            for code in self.config.cell_codes:
                self.store.facts[(day, code)] = self.fixture.prepare(code, [self.fixture.row(day=day)], day=day)
            self.store.dirty[day] = True
        self.batch = [self.store.facts[("2026-10-15", code)] for code in self.config.cell_codes]
        first = self.publish()
        second = self.publish()
        self.assertEqual(first["epoch"], second["epoch"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_manual_values_preserved_for_config_only_and_audited_before_refresh(self):
        self.publish()
        entry = self.store.publications[1]["snapshot"]["worksheets"]["summary"]["matrix"]["editable_inputs"][0]
        row, col = entry["row"] - 1, entry["column"] - 1
        self.adapters["summary"].values[row][col] = 333
        self.publish(configuration_only=True)
        self.assertEqual(self.adapters["summary"].values[row][col], 333)
        start = len(self.store.events)
        self.publish()
        self.assertNotEqual(self.adapters["summary"].values[row][col], 333)
        events = self.store.events[start:]
        self.assertLess(events.index("manual_audit"), events.index("sheets:summary"))

    def test_computed_tampering_and_wrong_adapter_workbook_fail_before_operation(self):
        self.publish()
        self.adapters["invoice"].values[2][1] = "tampered"
        with self.assertRaisesRegex(ValueError, "differs from its committed"):
            self.publish()
        self.assertEqual(self.store.operation, "IDLE")
        self.adapters["invoice"].spreadsheet_id = "other-workbook"
        with self.assertRaisesRegex(ValueError, "differs from approved IDs"):
            self.publish()

    def test_db_only_epoch_does_not_replace_existing_sheet_baseline(self):
        self.publish()
        self.store.publications[2] = {"snapshot": {}, "status": "DB_ONLY", "fingerprint": "db-only"}
        self.assertEqual(self.publish()["status"], "COMPLETE")

    def test_untrusted_detail_text_is_not_a_formula(self):
        from finpay_pipeline.cell_publication import matrix_write_requests
        request = matrix_write_requests([["=IMPORTDATA(\"https://example.test\")"]], 32, allow_formulas=False)[0]
        self.assertIn("stringValue", request["updateCells"]["rows"][0]["values"][0]["userEnteredValue"])

    def test_initial_empty_target_is_rechecked_after_all_guards(self):
        adapter = self.adapters["summary"]
        original = adapter.batch_update

        def inject(requests):
            result = original(requests)
            if any("addProtectedRange" in request for request in requests):
                self.adapters["invoice"].values = [["concurrent content"]]
            return result

        adapter.batch_update = inject
        with self.assertRaisesRegex(ValueError, "not empty"):
            self.publish()
        self.assertFalse(self.store.facts)
        self.assertEqual(self.adapters["invoice"].values, [["concurrent content"]])

    def test_large_formatting_is_chunked_and_progress_confirmed(self):
        from finpay_pipeline.cell_publication import _format_requests
        request = {"repeatCell": {"range": {"sheetId": 30, "startRowIndex": 0,
            "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"}}
        with patch("finpay_pipeline.cell_publication_v2._format_requests",
                   side_effect=lambda target, matrix: _format_requests(target, matrix) + [request] * 8000):
            self.assertEqual(self.publish()["status"], "COMPLETE")
        self.assertIn("format-0", self.store.events)
        self.assertIn("format-1", self.store.events)

    def test_tail_clear_readback_is_verified(self):
        from finpay_pipeline.cell_publication_v2 import _verify_values
        self.adapters["unusual"].values = [["header"], ["stale"]]
        with self.assertRaisesRegex(ValueError, "tail readback"):
            _verify_values(self.adapters["unusual"], {"values": [["header"]]},
                           {"values": [["header"], ["old"]]})

    def test_noop_rejects_style_drift_without_any_new_mutation(self):
        for kind in ("alignment", "fill", "border", "width", "merge", "freeze"):
            with self.subTest(kind=kind):
                self.setUp()
                self.publish()
                adapter = self.adapters["invoice"]
                if kind == "alignment":
                    adapter.styles[(2, 1)]["horizontalAlignment"] = "LEFT"
                elif kind == "fill":
                    adapter.styles[(3, 1)]["backgroundColor"] = {"red": 1, "green": 0, "blue": 1}
                elif kind == "border":
                    adapter.batch_update([{"updateBorders": {"range": {"sheetId": adapter.sheet_id,
                        "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 1,
                        "endColumnIndex": 2}, "top": {"style": "NONE"}}}])
                elif kind == "width":
                    adapter.widths[1] = 999
                elif kind == "merge":
                    adapter.merges.append({"sheetId": adapter.sheet_id, "startRowIndex": 2,
                        "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 4})
                else:
                    adapter.grid["frozenRowCount"] = 0
                events = list(self.store.events)
                with self.assertRaisesRegex(ValueError, "style"):
                    self.publish()
                self.assertEqual(self.store.events, events)
                self.assertEqual(self.store.operation, "IDLE")
                self.assertEqual(len(self.store.publications), 1)

    def test_first_write_cannot_complete_with_later_body_alignment_override(self):
        from finpay_pipeline.cell_invoice_sheets import invoice_format_requests

        def incorrect(sheet_id, matrix):
            return invoice_format_requests(sheet_id, matrix) + [{"repeatCell": {"range": {
                "sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                "startColumnIndex": 1, "endColumnIndex": 2}, "cell": {
                "userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                "fields": "userEnteredFormat.horizontalAlignment"}}]

        with patch("finpay_pipeline.cell_publication_v2.invoice_format_requests", side_effect=incorrect):
            with self.assertRaisesRegex(ValueError, "effective style"):
                self.publish()
        self.assertEqual(self.store.publications[1]["status"], "PENDING")
        self.assertEqual(self.store.operation, "IDLE")
        self.assertEqual(self.store.operation_error, "ValueError")

    def test_style_only_revision_publishes_then_conforming_noop_reuses_epoch(self):
        first = self.publish()
        values = {role: copy.deepcopy(adapter.values) for role, adapter in self.adapters.items()}
        with patch("finpay_pipeline.cell_publication_v2.STYLE_REVISION", "test-next-style-revision"):
            second = self.publish(configuration_only=True)
            self.assertNotEqual(first["epoch"], second["epoch"])
            self.assertEqual(values, {role: adapter.values for role, adapter in self.adapters.items()})
            formats = {role: len(adapter.events) for role, adapter in self.adapters.items()}
            self.assertEqual(second["epoch"], self.publish(configuration_only=True)["epoch"])
            self.assertEqual(formats, {role: len(adapter.events) for role, adapter in self.adapters.items()})
            self.assertEqual(len(self.store.facts), 2)

    def test_legacy_summary_migration_preserves_manual_cell_and_clears_old_header_tail(self):
        from finpay_pipeline.cell_calculation import build_cluster_report
        from finpay_pipeline.cell_contracts import fingerprint
        from finpay_pipeline.cell_publication import _format_requests, _protection_requests
        from finpay_pipeline.cell_sheets import render_cell_matrix
        self.publish()
        # Synthetic fixture for a genuinely committed old layout, not recovery code.
        publication = self.store.publications[1]
        saved = publication["snapshot"]["worksheets"]
        old = render_cell_matrix(build_cluster_report(self.config, self.batch, "2026-10-13"))
        saved["summary"].update(matrix=old, values=old["values"])
        for item in saved.values():
            item["matrix"].pop("style_contract", None)
        adapter = self.adapters["summary"]
        adapter.values = copy.deepcopy(old["values"])
        adapter.merges = []
        adapter.batch_update(_format_requests(self.roles["summary"].sheet_target(), old))
        guard = next(item["protectedRangeId"] for item in adapter.protections
                     if item["description"].endswith("COMPUTED"))
        adapter.batch_update(_protection_requests(self.roles["summary"].sheet_target(), old, adapter.metadata(), guard))
        digest = publication["fingerprint"] = fingerprint(publication["snapshot"])
        for target in self.adapters.values():
            target.metadata_entries[0]["metadataValue"] = json.dumps({"status": "COMPLETE", "epoch": 1, "fingerprint": digest})
        entry = next(item for item in old["editable_inputs"] if item["cell_code"] == "412111")
        adapter.values[entry["row"] - 1][entry["column"] - 1] = 333
        events = list(self.store.events)
        with self.assertRaisesRegex(ValueError, "configuration-only presentation migration"):
            self.publish()
        self.assertEqual(self.store.events, events)
        self.publish(configuration_only=True)
        migrated = self.store.latest_publication()["snapshot"]["worksheets"]["summary"]["matrix"]
        moved = next(item for item in migrated["editable_inputs"] if item["cell_code"] == "412111")
        self.assertEqual((entry["column"], moved["column"]), (8, 4))
        self.assertEqual(adapter.values[moved["row"] - 1][3], 333)
        self.assertTrue(all(item["endColumnIndex"] <= 10 for item in adapter.merges))
        self.assertTrue(all(value == "" for row in adapter.values for value in row[10:]))
        self.assertTrue(all(not value for (row, col), value in adapter.styles.items() if col >= 10))

    def test_pinned_reference_style_is_realized_in_fake_not_just_requested(self):
        rows = []
        for code in self.config.cell_codes:
            row = self.fixture.row()
            row["Transaction"] = "SYNTHETIC UNKNOWN LABEL"
            rows.append(self.fixture.prepare(code, [row]))
        self.batch = rows
        self.publish()
        invoice, detail = self.adapters["invoice"], self.adapters["unusual"]
        self.assertEqual(invoice.widths[1], 410)
        self.assertEqual(invoice.styles[(2, 1)]["horizontalAlignment"], "CENTER")
        self.assertEqual(invoice.styles[(2, 1)]["backgroundColor"], {"red": 209 / 255, "green": 232 / 255, "blue": 204 / 255})
        self.assertEqual(detail.styles[(2, 1)]["backgroundColor"], {"red": 229 / 255, "green": 240 / 255, "blue": 1})
        self.assertEqual([row[1] for row in detail.values[1:]], [1, 2])
        self.assertEqual(detail.widths[1], 70)

    def test_guard_timeout_after_apply_leaves_durable_unknown_without_staged_epoch(self):
        from finpay_pipeline.cell_publication import GUARD_PREFIX
        adapter = self.adapters["summary"]
        original = adapter.batch_update

        def uncertain(requests):
            result = original(requests)
            if any("addProtectedRange" in request for request in requests):
                raise TimeoutError("guard applied but acknowledgement lost")
            return result

        adapter.batch_update = uncertain
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.store.operation, "UNKNOWN")
        self.assertEqual(self.store.publications, {})
        for target in self.adapters.values():
            self.assertTrue(any(item.get("description", "").startswith(GUARD_PREFIX) for item in target.protections))
            self.assertEqual(target.values, [])

    def test_under_guard_style_failure_records_known_error_before_new_stage(self):
        from finpay_pipeline.cell_publication import GUARD_PREFIX
        self.publish()
        adapter = self.adapters["summary"]
        original = adapter.batch_update

        def drift(requests):
            result = original(requests)
            if any(request.get("updateProtectedRange", {}).get("protectedRange", {}).get("description", "")
                   .startswith(GUARD_PREFIX) for request in requests):
                self.adapters["invoice"].styles[(2, 1)]["horizontalAlignment"] = "LEFT"
            return result

        adapter.batch_update = drift
        with self.assertRaisesRegex(ValueError, "effective style"):
            self.publish()
        self.assertEqual(self.store.operation, "IDLE")
        self.assertEqual(self.store.operation_error, "ValueError")
        self.assertEqual(len(self.store.publications), 1)
        self.assertEqual(self.store.publications[1]["status"], "COMPLETE")

    def test_native_merged_headers_use_anchor_and_outer_edges(self):
        self.publish()
        adapter = self.adapters["summary"]
        page = next(adapter.read_style(2, 10))
        self.assertEqual(page["formats"][0][0]["borders"]["bottom"]["style"], "SOLID_MEDIUM")
        self.assertEqual(page["formats"][0][3]["borders"]["right"]["style"], "SOLID_MEDIUM")
        self.assertEqual(page["formats"][0][4], {})
        self.assertEqual(page["formats"][1][0], {})
        adapter.styles[(0, 0)]["textFormat"]["bold"] = False
        with self.assertRaisesRegex(ValueError, "style"):
            self.publish()

    def test_late_gutter_border_clear_is_rejected_with_native_shared_edge_model(self):
        from finpay_pipeline.cell_invoice_sheets import invoice_format_requests

        def incorrect(sheet_id, matrix):
            return invoice_format_requests(sheet_id, matrix) + [{"updateBorders": {"range": {
                "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": len(matrix["values"]),
                "startColumnIndex": 4, "endColumnIndex": 5}, "left": {"style": "NONE"},
                "right": {"style": "NONE"}}}]

        with patch("finpay_pipeline.cell_publication_v2.invoice_format_requests", side_effect=incorrect):
            with self.assertRaisesRegex(ValueError, "style"):
                self.publish()
        self.assertEqual(self.store.operation, "IDLE")
        self.assertEqual(self.store.publications[1]["status"], "PENDING")

    def test_logical_border_is_verified_even_when_one_neighbor_owns_the_line(self):
        first = self.publish()
        # Native GridData can put the same visible boundary on just one side.
        self.adapters["invoice"].styles[(2, 1)]["borders"].pop("top")
        self.assertEqual(self.publish()["epoch"], first["epoch"])
        # Clearing the physical boundary removes both owners and must still fail.
        self.adapters["invoice"].batch_update([{"updateBorders": {"range": {
            "sheetId": self.roles["invoice"].worksheet_id, "startRowIndex": 2,
            "endRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 2}, "top": {"style": "NONE"}}}])
        with self.assertRaisesRegex(ValueError, "border mismatch"):
            self.publish()

    def test_readback_timeout_after_acknowledged_mutations_is_pending_not_unknown(self):
        from unittest.mock import Mock
        self.adapters["summary"].read_style = Mock(side_effect=TimeoutError("read-only response lost"))
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.store.publications[1]["status"], "PENDING")
        self.assertEqual(self.store.operation, "IDLE")
        self.assertEqual(self.store.operation_error, "TimeoutError")

    def test_stored_v2_invoice_and_detail_contracts_keep_old_geometry(self):
        from finpay_pipeline.cell_publication_v2 import build_cell_cluster_matrices_v2, bind_cell_style_contract
        from finpay_pipeline.cell_invoice_sheets import invoice_format_requests
        from finpay_pipeline.cell_detail_sheets import detail_format_requests
        facts = []
        for day in ("2026-10-13", "2026-10-14"):
            for code in self.config.cell_codes:
                row = self.fixture.row(day=day)
                row["Transaction"] = "SYNTHETIC UNKNOWN"
                facts.append(self.fixture.prepare(code, [row], day=day))
        matrices = build_cell_cluster_matrices_v2(self.config, facts, "2026-10-14")
        old = copy.deepcopy(matrices["invoice"])
        old["layout_version"] = 2
        old["values"] = [[row[index] for index in (0, 1, 2, 3, 4, 7, 8, 9, 10)] for row in old["values"]]
        old["panels"][1].update(start_column=6, end_column=9)
        bind_cell_style_contract("invoice", old)
        requests = invoice_format_requests(31, old)
        self.assertFalse(any("mergeCells" in item for item in requests))
        self.assertTrue(any(item.get("updateDimensionProperties", {}).get("range", {}).get("startIndex") == 0
            and item["updateDimensionProperties"]["properties"]["pixelSize"] == 110 for item in requests))
        detail = copy.deepcopy(matrices["unusual"])
        detail["layout_version"] = 2
        detail.pop("day_separator_rows")
        bind_cell_style_contract("unusual", detail)
        requests = detail_format_requests(32, detail)
        self.assertEqual(detail["style_contract"]["widths"][1], 50)
        self.assertEqual([item["updateBorders"]["range"]["startRowIndex"] for item in requests
            if item.get("updateBorders", {}).get("top", {}).get("style") == "SOLID_MEDIUM"], [1])

    def test_independent_edit_policy_rejects_credit_column_or_non_summary_holes(self):
        from finpay_pipeline.cell_publication import _validate_input_policy
        from finpay_pipeline.cell_publication_v2 import build_cell_cluster_matrices_v2
        matrices = build_cell_cluster_matrices_v2(self.config, self.batch, "2026-10-13")
        summary = copy.deepcopy(matrices["summary"])
        summary["editable_inputs"][0]["column"] += 1
        with self.assertRaisesRegex(ValueError, "exact MANDIRI"):
            _validate_input_policy(summary)
        invoice = copy.deepcopy(matrices["invoice"])
        invoice["editable_inputs"] = copy.deepcopy(matrices["summary"]["editable_inputs"])
        with self.assertRaisesRegex(ValueError, "non-Summary"):
            _validate_input_policy(invoice)

    def test_v1_spreadsheet_or_wrong_sheet_title_rejected(self):
        with self.assertRaisesRegex(ValueError, "distinct from v1"):
            validate_output_targets(self.store.target_id, self.store.target_id,
                                    {"412110_B": self.config}, {"412110_B": self.roles})
        self.adapters["invoice"].title = "Legacy QRISDUWIT"
        with self.assertRaisesRegex(ValueError, "title does not match"):
            self.publish()


if __name__ == "__main__":
    unittest.main()
