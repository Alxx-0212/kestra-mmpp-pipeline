"""Fake Google/DB publication checks; no clients, secrets or network are used."""
import copy
import json
import unittest

from finpay_pipeline.cell_contracts import fingerprint
from finpay_pipeline.cell_calculation import CellReportConfig
from finpay_pipeline.cell_publication import (
    GUARD_PREFIX, MANAGED_PREFIX, CellSheetTarget, matrix_write_requests, publish_cell_cluster,
    _editors_match,
)
from finpay_pipeline.tests import test_cell_calculation as fixtures


class FakeStore:
    def __init__(self):
        self.cluster_id, self.target_id = "412110", "dummy-sheet-only"
        self.alive = True
        self.facts = {}
        self.dirty = {}
        self.publications = {}
        self.operation = None
        self.events = []
        self.fail_audit = False
        self.fail_complete = False

    def assert_alive(self):
        if not self.alive:
            raise RuntimeError("synthetic lock lost")

    def begin_target_operation(self, identity):
        if self.operation and self.operation[1] != "IDLE":
            raise RuntimeError("ambiguous prior target operation")
        self.operation = (identity, "IN_FLIGHT")
        self.events.append("intent")

    def finish_target_operation(self, identity, unknown_error=None):
        self.assert_alive()
        self.operation = (identity, "UNKNOWN" if unknown_error else "IDLE")

    def latest_publication(self):
        if not self.publications:
            return None
        epoch = max(self.publications)
        return {"epoch": epoch, **copy.deepcopy(self.publications[epoch])}

    def latest_report_date(self):
        return max((date for date, _ in self.facts), default=None)

    def dirty_dates(self, *, reset_only=False):
        return [day for day, reset in self.dirty.items() if reset or not reset_only]

    def current_source_rows(self, start, end):
        return [{"manifest": item["manifest"], "raw_rows": item["raw_rows"]}
                for (day, code), item in sorted(self.facts.items()) if start <= day <= end]

    def current_results(self, start, end):
        return [{"account": item["identity"]["account"], "cell_code": code, "report_date": date,
                 "generation_id": item["generation_id"], "calculation_id": item["calculation_id"],
                 "config_id": item["config_id"], "result": item["calculation"]}
                for (date, code), item in sorted(self.facts.items()) if start <= date <= end]

    def commit_and_stage(self, batch, cells, callback, write_gsheet, additional_batches=()):
        previous_facts = copy.deepcopy(self.facts)
        previous_dirty = copy.deepcopy(self.dirty)
        try:
            for cohort in [*additional_batches, batch]:
                for item in cohort:
                    key = (item["report_date"], item["identity"]["cell_code"])
                    old = self.facts.get(key)
                    if old is None or old["calculation_id"] != item["calculation_id"]:
                        reset = old is None or old["generation_id"] != item["generation_id"]
                        self.dirty[key[0]] = self.dirty.get(key[0], False) or reset
                    self.facts[key] = item
            render = callback(self)
            digest = fingerprint(render["snapshot"])
            previous = self.latest_publication()
            if previous and previous["digest"] == digest and not render["refresh_audit_required"]:
                return {"epoch": previous["epoch"], "fingerprint": digest, "status": previous["status"]}
            epoch = len(self.publications) + 1
            self.publications[epoch] = {"digest": digest, "snapshot": render["snapshot"], "status": "PENDING"}
            self.events.append("commit")
            return {"epoch": epoch, "fingerprint": digest, "status": "PENDING"}
        except Exception:
            self.facts = previous_facts
            self.dirty = previous_dirty
            raise

    def publication_snapshot(self, epoch):
        return copy.deepcopy(self.publications[epoch])

    def record_manual_reset(self, *args):
        self.events.append("audit")
        if self.fail_audit:
            raise RuntimeError("synthetic audit failure")

    def begin_sheet_write(self, epoch):
        self.publications[epoch]["status"] = "IN_FLIGHT"
        self.events.append("begin_write")

    def confirm_chunk(self, epoch, chunk):
        self.events.append(chunk)

    def mark_unknown(self, epoch, error_class):
        self.assert_alive()
        self.publications[epoch]["status"] = "UNKNOWN"

    def complete(self, epoch, verified_fingerprint):
        if self.fail_complete:
            raise RuntimeError("synthetic completion DB failure")
        assert self.publications[epoch]["digest"] == verified_fingerprint
        self.publications[epoch]["status"] = "COMPLETE"
        self.dirty.clear()
        self.events.append("complete")


class FakeAdapter:
    def __init__(self, store):
        self.store = store
        self.spreadsheet_id, self.worksheet_id = store.target_id, 7
        self.values = []
        self.protections = []
        self.developer_metadata = []
        self.notes = {}
        self.properties = {"sheetId": 7, "gridProperties": {"rowCount": 1000, "columnCount": 27}}
        self.next_id = 10
        self.calls = []
        self.fail_guard = False
        self.fail_data = False
        self.corrupt_readback = False
        self.lose_lock_after_data = False
        self.ignore_final_protections = False
        self.update_count = 0
        self.fail_after_update = None
        self.fail_restore = False
        self.server_owners = []
        self.omit_default_warning = False
        self.narrow_guard_readback = False
        self.inject_bank_hole = False

    def metadata(self):
        protections = copy.deepcopy(self.protections)
        for item in protections:
            if self.narrow_guard_readback and item.get("description", "").startswith(GUARD_PREFIX):
                item["range"] = {"sheetId": 7, "startRowIndex": 0, "endRowIndex": 1}
            if self.inject_bank_hole and item.get("description") == MANAGED_PREFIX + "BANK INPUT":
                item["unprotectedRanges"] = [dict(item["range"])]
            if self.server_owners:
                item["editors"]["users"] = sorted(set(item["editors"]["users"] + self.server_owners))
            if self.omit_default_warning and item.get("warningOnly") is False:
                item.pop("warningOnly")
        return {"properties": copy.deepcopy(self.properties),
                "protectedRanges": protections,
                "developerMetadata": copy.deepcopy(self.developer_metadata),
                "implicitOwnerEmails": list(self.server_owners)}

    def marker(self):
        return json.loads(self.developer_metadata[0]["metadataValue"])

    def read(self, rows, columns):
        values = copy.deepcopy([row[:columns] for row in self.values[:rows]])
        if self.corrupt_readback and values:
            values[0][0] = "CORRUPTED GENERATION"
        while values and not any(value != "" for value in values[-1]):
            values.pop()
        for row in values:
            while row and row[-1] == "":
                row.pop()
        return values

    def batch_update(self, requests):
        self.calls.append(copy.deepcopy(requests))
        if self.fail_restore and any("deleteProtectedRange" in request or
            request.get("updateProtectedRange", {}).get("protectedRange", {}).get("description", "").startswith(MANAGED_PREFIX)
            for request in requests):
            raise TimeoutError("synthetic restoration outcome unknown")
        replies = []
        for request in requests:
            if "createDeveloperMetadata" in request:
                item = copy.deepcopy(request["createDeveloperMetadata"]["developerMetadata"])
                item["metadataId"] = self.next_id
                self.next_id += 1
                self.developer_metadata.append(item)
                replies.append({"createDeveloperMetadata": {"developerMetadata": item}})
            elif "updateDeveloperMetadata" in request:
                update = request["updateDeveloperMetadata"]
                key = update["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
                item = next(value for value in self.developer_metadata if value["metadataId"] == key)
                item["metadataValue"] = update["developerMetadata"]["metadataValue"]
                replies.append({})
            elif "updateSheetProperties" in request:
                for key, value in request["updateSheetProperties"]["properties"].items():
                    if key == "gridProperties":
                        self.properties[key].update(value)
                    else:
                        self.properties[key] = copy.deepcopy(value)
                replies.append({})
            elif "addProtectedRange" in request:
                item = copy.deepcopy(request["addProtectedRange"]["protectedRange"])
                if item["description"].startswith(GUARD_PREFIX) and self.fail_guard:
                    raise TimeoutError("synthetic guard outcome unknown")
                if item["range"] == {"sheetId": 7} and any(p["range"] == {"sheetId": 7} for p in self.protections):
                    raise ValueError("Google allows only one sheet-level protection")
                item["protectedRangeId"] = self.next_id
                self.next_id += 1
                if not (self.ignore_final_protections and item["description"].startswith(MANAGED_PREFIX)):
                    self.protections.append(item)
                replies.append({"addProtectedRange": {"protectedRange": item}})
            elif "updateProtectedRange" in request:
                update = request["updateProtectedRange"]
                values = update["protectedRange"]
                item = next(p for p in self.protections if p["protectedRangeId"] == values["protectedRangeId"])
                if values.get("description", "").startswith(GUARD_PREFIX) and self.fail_guard:
                    raise TimeoutError("synthetic guard outcome unknown")
                if not (self.ignore_final_protections and values.get("description", "").startswith(MANAGED_PREFIX)):
                    for key in update["fields"].split(","):
                        item[key] = copy.deepcopy(values[key])
                replies.append({})
            elif "deleteProtectedRange" in request:
                key = request["deleteProtectedRange"]["protectedRangeId"]
                self.protections = [item for item in self.protections if item["protectedRangeId"] != key]
                replies.append({})
            elif "updateCells" in request:
                if request["updateCells"]["fields"] == "note":
                    self.notes["A1"] = request["updateCells"]["rows"][0]["values"][0]["note"]
                    replies.append({})
                    continue
                self.update_count += 1
                if self.fail_data or (self.fail_after_update is not None and self.update_count > self.fail_after_update):
                    raise TimeoutError("synthetic data outcome unknown")
                update = request["updateCells"]
                grid = update["range"]
                self.store.events.append("google_data")
                while len(self.values) < grid["endRowIndex"]:
                    self.values.append([""] * 27)
                for index in range(grid["startRowIndex"], grid["endRowIndex"]):
                    local = index - grid["startRowIndex"]
                    source = update["rows"][local]["values"] if local < len(update["rows"]) else []
                    for column in range(grid["startColumnIndex"], grid["endColumnIndex"]):
                        value = source[column].get("userEnteredValue", {}) if column < len(source) else {}
                        self.values[index][column] = next(iter(value.values())) if value else ""
                if self.lose_lock_after_data:
                    self.store.alive = False
                replies.append({})
            else:
                replies.append({})
        return {"replies": replies}


class CellPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CellCalculationTests()
        fixture.setUp()
        self.fixture, self.config = fixture, fixture.config
        self.batch = [fixture.prepare(code, [fixture.row()]) for code in self.config.cell_codes]
        self.store = FakeStore()
        self.adapter = FakeAdapter(self.store)
        self.target = CellSheetTarget("dummy-sheet-only", 7, "writer@example.test", ("finance@example.test",))

    def publish(self, **kwargs):
        return publish_cell_cluster(
            self.store, self.target, {self.target}, self.adapter, self.batch, self.config,
            changed_dates=["2026-10-13"], **kwargs,
        )

    def test_success_lock_intent_commit_readback_and_protections(self):
        result = self.publish(rows_per_request=12)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(self.store.operation[1], "IDLE")
        self.assertEqual(self.store.publications[1]["status"], "COMPLETE")
        self.assertLess(self.store.events.index("intent"), self.store.events.index("google_data"))
        self.assertLess(self.store.events.index("commit"), self.store.events.index("google_data"))
        self.assertFalse(any(item["description"].startswith(GUARD_PREFIX) for item in self.adapter.protections))
        self.assertEqual(self.adapter.marker(), {"status": "COMPLETE", "epoch": 1, "fingerprint": result["fingerprint"]})
        self.assertEqual(self.adapter.values[0][0], "REPORT DATE")
        self.assertEqual(self.adapter.properties["gridProperties"]["frozenRowCount"], 2)
        self.assertEqual(self.adapter.properties["gridProperties"]["frozenColumnCount"], 3)

    def test_guard_timeout_blocks_target_before_any_publication(self):
        self.adapter.fail_guard = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.store.operation[1], "UNKNOWN")
        self.assertFalse(self.store.publications)
        with self.assertRaisesRegex(RuntimeError, "ambiguous prior"):
            self.publish()

    def test_data_timeout_marks_unknown_and_retains_guard(self):
        self.adapter.fail_data = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.store.publications[1]["status"], "UNKNOWN")
        self.assertTrue(any(item["description"].startswith(GUARD_PREFIX) for item in self.adapter.protections))

    def test_partial_chunk_timeout_retains_pending_marker_and_confirmed_prefix(self):
        self.adapter.fail_after_update = 1  # First data chunk succeeded.
        with self.assertRaises(TimeoutError):
            self.publish(rows_per_request=5)
        self.assertEqual(self.store.publications[1]["status"], "UNKNOWN")
        self.assertEqual(self.adapter.marker()["status"], "PENDING")
        self.assertIn("rows-0", self.store.events)
        self.assertNotIn("rows-1", self.store.events)

    def test_restoration_timeout_is_unknown_not_success(self):
        self.adapter.fail_restore = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.store.operation[1], "UNKNOWN")
        self.assertEqual(self.store.publications[1]["status"], "UNKNOWN")
        self.assertNotIn("complete", self.store.events)

    def test_readback_mismatch_never_records_complete(self):
        self.adapter.corrupt_readback = True
        with self.assertRaisesRegex(ValueError, "readback mismatch"):
            self.publish()
        self.assertNotIn("complete", self.store.events)
        self.assertEqual(self.store.publications[1]["status"], "UNKNOWN")

    def test_lock_loss_stops_further_google_calls_and_leaves_inflight_intent(self):
        self.adapter.lose_lock_after_data = True
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.store.operation[1], "IN_FLIGHT")
        self.assertNotIn("complete", self.store.events)
        self.assertEqual(len(self.adapter.calls), 3)

    def test_exact_retry_verifies_existing_generation_without_rewriting_data(self):
        first = self.publish()
        data_events = self.store.events.count("google_data")
        second = self.publish()
        self.assertEqual(first, second)
        # Metadata/protection restoration repeats, not the data matrix.
        self.assertEqual(self.store.events.count("google_data"), data_events)

    def test_manual_reset_audit_precedes_new_data_and_failure_blocks_write(self):
        self.publish()
        matrix = self.store.publications[1]["snapshot"]["matrix"]
        entry = next(item for item in matrix["editable_inputs"] if item["cell_code"] == "412111")
        self.adapter.values[entry["row"] - 1][entry["column"] - 1] = 999
        self.store.fail_audit = True
        before = self.store.events.count("google_data")
        with self.assertRaisesRegex(RuntimeError, "audit failure"):
            self.publish()
        self.assertEqual(self.store.events.count("google_data"), before)
        self.assertIn("audit", self.store.events)
        self.assertEqual(self.store.operation[1], "UNKNOWN")

    def test_complete_status_db_failure_remains_unknown_for_operator_recovery(self):
        self.store.fail_complete = True
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.adapter.marker()["status"], "COMPLETE")
        self.assertIn("Sheet contents VERIFIED", self.adapter.notes["A1"])
        self.assertIn("Database completion is tracked separately", self.adapter.notes["A1"])
        self.assertEqual(self.store.publications[1]["status"], "UNKNOWN")
        self.assertEqual(self.store.operation[1], "UNKNOWN")

    def test_missing_restored_protections_cannot_report_complete(self):
        self.adapter.ignore_final_protections = True
        with self.assertRaisesRegex(ValueError, "protection"):
            self.publish()
        self.assertNotIn("complete", self.store.events)

    def test_refresh_reuses_the_single_sheet_level_protection(self):
        self.publish()
        first = next(item["protectedRangeId"] for item in self.adapter.protections
                     if item["description"] == MANAGED_PREFIX + "COMPUTED")
        self.publish()
        full_sheet = [item for item in self.adapter.protections if item["range"] == {"sheetId": 7}]
        self.assertEqual(len(full_sheet), 1)
        self.assertEqual(full_sheet[0]["protectedRangeId"], first)

    def test_narrowed_managed_sheet_guard_is_rejected_before_mutation(self):
        self.publish()
        item = next(value for value in self.adapter.protections if value["description"] == MANAGED_PREFIX + "COMPUTED")
        item["range"] = {"sheetId": 7, "startRowIndex": 0, "endRowIndex": 1}
        before = len(self.adapter.calls)
        with self.assertRaisesRegex(ValueError, "whole approved worksheet"):
            self.publish()
        self.assertEqual(len(self.adapter.calls), before)

    def test_narrowed_guard_readback_and_bank_holes_never_confirm_complete(self):
        self.adapter.narrow_guard_readback = True
        with self.assertRaisesRegex(ValueError, "temporary input protection"):
            self.publish()
        self.assertFalse(self.store.publications)
        self.store = FakeStore()
        self.adapter = FakeAdapter(self.store)
        self.adapter.inject_bank_hole = True
        with self.assertRaisesRegex(ValueError, "Finance input editor protections"):
            self.publish()
        self.assertNotIn("complete", self.store.events)

    def test_metadata_and_formatting_do_not_consume_visible_header_rows(self):
        self.publish()
        self.assertEqual(len(self.adapter.developer_metadata), 1)
        self.assertEqual(self.adapter.developer_metadata[0]["location"], {"sheetId": 7})
        self.assertNotIn("PUBLICATION", [value for row in self.adapter.values for value in row])
        requests = [request for batch in self.adapter.calls for request in batch]
        merges = [item["mergeCells"]["range"] for item in requests if "mergeCells" in item]
        self.assertTrue(merges)
        for grid in merges:
            self.assertIn((grid["startRowIndex"], grid["endRowIndex"]), ((0, 1), (0, 2)))
            self.assertFalse(grid["startColumnIndex"] < 3 < grid["endColumnIndex"])
        self.assertTrue(any(item.get("updateBorders", {}).get("right", {}).get("style") == "SOLID_MEDIUM"
                            for item in requests))

    def test_google_implicit_owner_and_omitted_false_are_not_new_editor_grants(self):
        self.adapter.server_owners = ["owner@example.test"]
        self.adapter.omit_default_warning = True
        self.assertEqual(self.publish()["status"], "COMPLETE")
        self.assertTrue(_editors_match({"users": ["writer@example.test", "owner@example.test"]},
                                       ["writer@example.test"], ["owner@example.test"]))
        self.assertFalse(_editors_match({"users": ["writer@example.test", "other@example.test"]},
                                        ["writer@example.test"], ["owner@example.test"]))

    def test_balance_change_recalculates_history_without_resetting_untouched_manual_day(self):
        for day in ("2026-10-13", "2026-10-14", "2026-10-15"):
            for code in self.config.cell_codes:
                self.store.facts[(day, code)] = self.fixture.prepare(code, [self.fixture.row(day=day)], day)
        self.publish()
        old_ids = {key: value["generation_id"] for key, value in self.store.facts.items()}
        original = self.store.publications[1]["snapshot"]["matrix"]
        bank = next(item for item in original["editable_inputs"]
                    if item["report_date"] == "2026-10-13" and item["cell_code"] == "412111")
        self.adapter.values[bank["row"] - 1][bank["column"] - 1] = 333
        previous_bank = next(item for item in original["editable_inputs"]
                             if item["report_date"] == "2026-10-14" and item["cell_code"] == "412111")
        self.adapter.values[previous_bank["row"] - 1][previous_bank["column"] - 1] = 444
        changed = CellReportConfig(
            self.config.account, self.config.cell_codes, self.config.first_report_date,
            self.config.opening_boundary, {"412111": 1500, "412112": 2000}, self.config.roster_version,
        )
        batch = [self.fixture.prepare(code, [self.fixture.row(day="2026-10-15")],
                                      "2026-10-15", config=changed) for code in changed.cell_codes]
        result = publish_cell_cluster(
            self.store, self.target, {self.target}, self.adapter, batch, changed,
            changed_dates=["2026-10-15"],
            configuration_only=True,
        )
        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(all(item["config_id"] == fingerprint(changed.to_dict()) for item in self.store.facts.values()))
        self.assertEqual({key: value["generation_id"] for key, value in self.store.facts.items()}, old_ids)
        self.assertEqual(self.adapter.values[bank["row"] - 1][bank["column"] - 1], 333)
        self.assertEqual(self.adapter.values[previous_bank["row"] - 1][previous_bank["column"] - 1], 444)
        self.assertEqual(self.adapter.values[2][9], 1500)
        preview = self.store.publications[result["epoch"]]["snapshot"]["matrix"]
        for index, day in enumerate(("2026-10-13", "2026-10-14", "2026-10-15"), 1):
            bounds = next(item for item in preview["date_ranges"] if item["report_date"] == day)
            saldos = [row[5] for row in preview["numeric_preview"][bounds["start_row"] - 1:bounds["end_row"]]
                      if type(row[5]) is int]
            self.assertEqual(saldos[-1], 3500 + index * 200)

    def test_wrong_target_and_existing_unmanaged_sheet_are_rejected(self):
        self.adapter.worksheet_id = 9
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.adapter.calls)
        self.adapter.worksheet_id = 7
        self.adapter.values = [["LEGACY INVOICE"]]
        with self.assertRaisesRegex(ValueError, "non-empty worksheet"):
            self.publish()
        self.assertFalse(self.store.publications)

    def test_typed_chunks_keep_formulas_literal_headers_and_exact_numbers(self):
        requests = matrix_write_requests([["DATE", "=SUM(K5,R5)", 100, ""], ["OTHER", "", 0, ""]], 7,
                                        rows_per_request=1)
        self.assertEqual(len(requests), 2)
        values = requests[0]["updateCells"]["rows"][0]["values"]
        self.assertEqual(values[0]["userEnteredValue"], {"stringValue": "DATE"})
        self.assertEqual(values[1]["userEnteredValue"], {"formulaValue": "=SUM(K5,R5)"})
        self.assertEqual(values[2]["userEnteredValue"], {"numberValue": 100})
        self.assertEqual(values[3]["userEnteredValue"], {})


    def test_style_reader_is_paged_identity_bound_and_excludes_values(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from finpay_pipeline.cell_publication import ExistingSheetAdapter
        calls = []

        def metadata(*, params):
            calls.append(params)
            if "ranges" not in params:
                return {"spreadsheetId": "approved-book", "sheets": [{
                    "properties": {"sheetId": 7}, "merges": [{"sheetId": 7, "startRowIndex": 0,
                        "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2}], "bandedRanges": []}]}
            start = 0 if params["ranges"].endswith("A1:B100") else 100
            return {"spreadsheetId": "approved-book", "properties": {"defaultFormat": {}}, "sheets": [
                {"properties": {"sheetId": 7, "gridProperties": {"frozenRowCount": 2}},
                 "data": [{"startRow": start, "startColumn": 0,
                    "columnMetadata": [{"pixelSize": 110}, {"pixelSize": 410}],
                    "rowMetadata": [{"pixelSize": 21}], "rowData": [{"values": [
                        {"effectiveFormat": {"horizontalAlignment": "CENTER"}}]}]}]}]}

        workbook = SimpleNamespace(id="approved-book", fetch_sheet_metadata=Mock(side_effect=metadata))
        adapter = ExistingSheetAdapter(workbook, SimpleNamespace(id=7, title="412110 Invoice"))
        pages = list(adapter.read_style(105, 2))
        self.assertEqual([len(page["formats"]) for page in pages], [100, 5])
        self.assertNotIn("ranges", calls[0])
        self.assertEqual(calls[1]["ranges"], "'412110 Invoice'!A1:B100")
        self.assertEqual(calls[2]["ranges"], "'412110 Invoice'!A101:B105")
        self.assertEqual(pages[0]["merges"], pages[1]["merges"])
        self.assertEqual(len(pages[1]["merges"]), 1)
        self.assertEqual(pages[0]["widths"], [110, 410])
        self.assertEqual(pages[1]["formats"][0][0]["horizontalAlignment"], "CENTER")
        for params in calls:
            self.assertFalse(any(name in params["fields"] for name in
                                 ("userEnteredValue", "effectiveValue", "formattedValue")))
        workbook.fetch_sheet_metadata.side_effect = None
        workbook.fetch_sheet_metadata.return_value = {"spreadsheetId": "different-book"}
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            list(adapter.read_style(1, 1))
        with self.assertRaisesRegex(ValueError, "governed dimensions"):
            list(adapter.read_style(100_001, 2))


if __name__ == "__main__":
    unittest.main()
