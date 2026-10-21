"""Offline coverage for configurable cell-format spreadsheet targets."""
import json
import unittest
from unittest.mock import patch

from finpay_pipeline.cell_format import bulk_pipeline, pipeline, settings
from finpay_pipeline.cell_format.settings import selected_clusters
from finpay_pipeline.cell_workflow import make_cell_sheet_client
from finpay_pipeline.cell_publication_v2 import ROLES, cell_worksheet_title


CLUSTER = "412110"
ACCOUNT = CLUSTER + "_B"
BASE_ENV = {
    "CELL_FORMAT_LEGACY_SPREADSHEET_ID": "legacy-workbook",
    "CELL_FORMAT_WRITER_EMAIL": "writer@example.test",
    "CELL_FORMAT_FINANCE_EMAILS": "finance-a@example.test, finance-b@example.test",
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeWorksheet:
    def __init__(self, worksheet_id, title, values=None):
        self.id = worksheet_id
        self.title = title
        self.values = [] if values is None else values
        self.protected_ranges = []
        self.developer_metadata = []
        self.row_count = 1000
        self.column_count = 26

    def get(self, range_name, value_render_option=None):
        return self.values


class FakeHttpClient:
    def __init__(self, request_handler):
        self.request_handler = request_handler

    def request(self, method, url, **kwargs):
        return self.request_handler(method, url, **kwargs)


class FakeWorkbook:
    def __init__(self, spreadsheet_id, title, worksheets=(), permissions=()):
        self.id = spreadsheet_id
        self.title = title
        self._worksheets = list(worksheets)
        self.permissions = list(permissions)
        self.shares = []
        self.add_requests = []
        self.fail_add = None
        self.next_sheet_id = 1000
        self.client = FakeHttpClient(self._metadata_request)

    def _metadata_request(self, method, url, **kwargs):
        if method != "get" or url != f"https://www.googleapis.com/drive/v3/files/{self.id}":
            raise AssertionError("unexpected fake Drive metadata request")
        return FakeResponse({"id": self.id, "owners": []})

    def fetch_sheet_metadata(self):
        return {
            "spreadsheetId": self.id,
            "properties": {"title": self.title},
            "sheets": [{
                "properties": {
                    "sheetId": item.id, "title": item.title,
                    "gridProperties": {"rowCount": item.row_count, "columnCount": item.column_count},
                },
                "protectedRanges": item.protected_ranges,
                "developerMetadata": item.developer_metadata,
            }
                       for item in self._worksheets],
        }

    def worksheets(self):
        return list(self._worksheets)

    def get_worksheet_by_id(self, worksheet_id):
        return next((item for item in self._worksheets if item.id == worksheet_id), None)

    def batch_update(self, body):
        properties = body["requests"][0]["addSheet"]["properties"]
        self.add_requests.append(dict(properties))
        if self.fail_add == "before":
            self.fail_add = None
            raise TimeoutError("synthetic addSheet timeout")
        if any(item.title == properties["title"] for item in self._worksheets):
            raise ValueError("duplicate worksheet title")
        worksheet_id = properties.get("sheetId")
        if worksheet_id is None:
            worksheet_id = self.next_sheet_id
            self.next_sheet_id += 1
        if any(item.id == worksheet_id for item in self._worksheets):
            raise ValueError("duplicate worksheet ID")
        worksheet = FakeWorksheet(worksheet_id, properties["title"])
        self._worksheets.append(worksheet)
        if self.fail_add == "after":
            self.fail_add = None
            raise TimeoutError("synthetic addSheet response timeout")
        return {"replies": [{"addSheet": {"properties": {"sheetId": worksheet_id}}}]}

    def share(self, email, *, perm_type, role, notify):
        self.shares.append((email, perm_type, role, notify))
        self.permissions.append({"emailAddress": email, "role": role})

    def list_permissions(self):
        return list(self.permissions)


class FakeClient:
    def __init__(self, workbooks=()):
        self.workbooks = {book.id: book for book in workbooks}
        self.create_calls = []
        self.create_mode = None
        self.next_file_id = 1
        self.drive_queries = []
        self.http_client = FakeHttpClient(self._request)

    def _request(self, method, url, *, params):
        self.drive_queries.append((method, url, dict(params)))
        title = params["q"].split("'", 2)[1].replace("\\'", "'").replace("\\\\", "\\")
        files = [
            {"id": book.id, "name": book.title, "mimeType": "application/vnd.google-apps.spreadsheet"}
            for book in self.workbooks.values() if book.title == title
        ]
        return FakeResponse({"files": files, "incompleteSearch": False})

    def open_by_key(self, spreadsheet_id):
        return self.workbooks[spreadsheet_id]

    def create(self, title):
        self.create_calls.append(title)
        if self.create_mode == "before":
            raise TimeoutError("synthetic create timeout")
        spreadsheet_id = f"created-{self.next_file_id}"
        self.next_file_id += 1
        workbook = FakeWorkbook(
            spreadsheet_id, title,
            [FakeWorksheet(0, "Sheet1")],
            [{"emailAddress": "writer@example.test", "role": "owner"}],
        )
        self.workbooks[spreadsheet_id] = workbook
        if self.create_mode == "after":
            raise TimeoutError("synthetic create response timeout")
        if self.create_mode == "duplicates":
            duplicate = FakeWorkbook(
                f"created-{self.next_file_id}", title,
                permissions=[{"emailAddress": "writer@example.test", "role": "owner"}],
            )
            self.next_file_id += 1
            self.workbooks[duplicate.id] = duplicate
            raise TimeoutError("synthetic duplicate create outcome")
        return workbook


class MemoryOperationStore:
    def __init__(self, conflicting_targets=()):
        self.operations = {}
        self.conflicting_targets = set(conflicting_targets)

    def assert_cluster_target(self, target_id):
        if target_id is None and self.conflicting_targets:
            raise ValueError("cluster already has Sheet publication history in another workbook")
        if any(existing != target_id for existing in self.conflicting_targets):
            raise ValueError("cluster already has Sheet publication history in another workbook")

    def assert_alive(self):
        return None

    def begin_target_operation(self, operation_id):
        self.operations[operation_id] = "IN_FLIGHT"

    def finish_target_operation(self, operation_id, unknown_error=None):
        self.operations[operation_id] = "UNKNOWN" if unknown_error else "IDLE"


def _configuration(**overrides):
    environment = {**BASE_ENV, "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports"}
    environment.update(overrides)
    return settings.sheet_configuration(environment)


class CellSheetTargetTests(unittest.TestCase):
    def test_env_values_override_constants_and_name_defaults_to_create(self):
        with patch.object(settings, "CELL_FORMAT_SPREADSHEET_ID", "constant-id"), \
                patch.object(settings, "CELL_FORMAT_WRITER_EMAIL", "constant@example.test"):
            config = settings.sheet_configuration({
                **BASE_ENV,
                "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports",
                "CELL_FORMAT_SPREADSHEET_ID": "environment-id",
                "CELL_FORMAT_WRITER_EMAIL": "writer@example.test",
                "CELL_FORMAT_FINANCE_EMAILS": '["one@example.test", "two@example.test"]',
            })
            fallback = settings.sheet_configuration(BASE_ENV)
        self.assertEqual(config.spreadsheet_id, "environment-id")
        self.assertEqual(config.finance_emails, ("one@example.test", "two@example.test"))
        self.assertTrue(config.create_spreadsheet_if_missing)
        self.assertIsNone(config.worksheet_ids)
        self.assertEqual(fallback.spreadsheet_id, "constant-id")

    def test_explicit_workbook_id_is_opened_by_key_and_title_must_match(self):
        workbook = FakeWorkbook("selected", "Renamed Workbook")
        client = FakeClient([workbook])
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="selected")
        with self.assertRaisesRegex(ValueError, "exact title"):
            pipeline.resolve_cell_workbook(client, config)
        self.assertEqual(client.drive_queries, [])
        self.assertEqual(client.create_calls, [])

    def test_name_only_preflight_needs_no_worksheet_ids_or_google_client(self):
        clusters = selected_clusters("ALL")[:2]
        config = settings.validate_sheet_target_group(
            list(reversed(clusters)),
            environment={**BASE_ENV, "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports"},
        )
        self.assertIsNone(config.spreadsheet_id)
        self.assertIsNone(config.worksheet_ids)

    def test_daily_and_bulk_range_validation_accept_name_without_tab_ids(self):
        clusters = selected_clusters("ALL")[:2]
        environment = {**BASE_ENV, "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports"}
        with patch.dict("os.environ", environment, clear=True):
            daily = pipeline.validate_sheet_range(
                settings.REPORTING_START_DATE, settings.REPORTING_START_DATE,
                ", ".join(reversed(clusters)),
            )
            bulk = bulk_pipeline.validate_cell_bulk_sheet_range(
                settings.REPORTING_START_DATE, settings.REPORTING_START_DATE,
                ", ".join(reversed(clusters)),
            )
        self.assertEqual(daily["clusters"], list(clusters))
        self.assertEqual(bulk["clusters"], list(clusters))

    def test_target_configuration_rejects_invalid_identity_and_editor_settings(self):
        with self.assertRaisesRegex(ValueError, "distinct from the legacy"):
            settings.sheet_configuration({
                **BASE_ENV,
                "CELL_FORMAT_SPREADSHEET_ID": "legacy-workbook",
            })
        for value in ("", "not-an-email", "a@example.test,a@example.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.sheet_configuration({
                    **BASE_ENV,
                    "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports",
                    "CELL_FORMAT_FINANCE_EMAILS": value,
                })
        with self.assertRaisesRegex(ValueError, "must be the string true or false"):
            settings.sheet_configuration({
                **BASE_ENV,
                "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports",
                "CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING": "yes",
            })

    def test_cluster_selector_trims_sorts_and_rejects_duplicates_or_unknowns(self):
        clusters = selected_clusters("ALL")
        first, second = clusters[:2]
        self.assertEqual(
            selected_clusters(f" {second}, {first} "), tuple(sorted((first, second), key=int)),
        )
        for value in (f"{first},{first}", f"{first},999999", f"{first},", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                selected_clusters(value)
        with self.assertRaisesRegex(ValueError, "exactly one cluster"):
            settings.account_settings(f"{first},{second}")

    def test_exact_title_lookup_rejects_duplicates_and_does_not_create(self):
        first = FakeWorkbook("first", "Cell Reports")
        second = FakeWorkbook("second", "Cell Reports")
        client, store = FakeClient([first, second]), MemoryOperationStore()
        with self.assertRaisesRegex(ValueError, "multiple Google spreadsheets"):
            pipeline.resolve_cell_workbook(client, _configuration(), store)
        self.assertEqual(client.create_calls, [])
        self.assertEqual(store.operations, {})
        self.assertTrue(client.drive_queries)
        self.assertIn("drive/v3/files", client.drive_queries[0][1])

    def test_named_workbook_creation_checks_cluster_history_before_create(self):
        client = FakeClient()
        store = MemoryOperationStore(conflicting_targets={"previous-workbook"})
        with self.assertRaisesRegex(ValueError, "another workbook"):
            pipeline.resolve_cell_workbook(
                client, _configuration(), store, cluster_id=CLUSTER,
            )
        self.assertEqual(client.create_calls, [])
        self.assertEqual(store.operations, {})

    def test_existing_named_workbook_is_reused_without_create_or_share(self):
        workbook = FakeWorkbook(
            "existing", "Cell Reports",
            permissions=[{"emailAddress": "writer@example.test", "role": "owner"}],
        )
        client = FakeClient([workbook])
        resolved, spreadsheet_id, created = pipeline.resolve_cell_workbook(
            client, _configuration(), MemoryOperationStore(),
        )
        self.assertIs(resolved, workbook)
        self.assertEqual(spreadsheet_id, "existing")
        self.assertFalse(created)
        self.assertEqual(client.create_calls, [])
        self.assertEqual(workbook.shares, [])

    def test_missing_named_workbook_is_created_and_shared_inside_journal(self):
        client, store = FakeClient(), MemoryOperationStore()
        workbook, spreadsheet_id, created = pipeline.resolve_cell_workbook(
            client, _configuration(), store,
        )
        self.assertTrue(created)
        self.assertEqual(spreadsheet_id, workbook.id)
        self.assertEqual(
            workbook.shares,
            [("finance-a@example.test", "user", "writer", False),
             ("finance-b@example.test", "user", "writer", False)],
        )
        self.assertEqual(list(store.operations.values()), ["IDLE"])

    def test_ambiguous_file_create_does_not_share_a_concurrent_exact_title_match(self):
        client, store = FakeClient(), MemoryOperationStore()
        client.create_mode = "after"
        with self.assertRaisesRegex(RuntimeError, "blocked for operator reconciliation"):
            pipeline.resolve_cell_workbook(client, _configuration(), store)
        self.assertEqual(client.create_calls, ["Cell Reports"])
        self.assertEqual(client.workbooks["created-1"].shares, [])
        self.assertEqual(list(store.operations.values()), ["UNKNOWN"])

    def test_unresolved_file_create_marks_unknown_and_fails_closed(self):
        client, store = FakeClient(), MemoryOperationStore()
        client.create_mode = "before"
        with self.assertRaisesRegex(RuntimeError, "blocked for operator reconciliation"):
            pipeline.resolve_cell_workbook(client, _configuration(), store)
        self.assertEqual(list(store.operations.values()), ["UNKNOWN"])

        duplicate_client, duplicate_store = FakeClient(), MemoryOperationStore()
        duplicate_client.create_mode = "duplicates"
        with self.assertRaisesRegex(RuntimeError, "blocked for operator reconciliation"):
            pipeline.resolve_cell_workbook(duplicate_client, _configuration(), duplicate_store)
        self.assertEqual(list(duplicate_store.operations.values()), ["UNKNOWN"])

    def test_disabled_file_creation_fails_without_journal_or_create(self):
        client, store = FakeClient(), MemoryOperationStore()
        config = _configuration(CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING="false")
        with self.assertRaisesRegex(FileNotFoundError, "creation is disabled"):
            pipeline.resolve_cell_workbook(client, config, store)
        self.assertEqual(client.create_calls, [])
        self.assertEqual(store.operations, {})

    def test_alias_tabs_are_created_once_then_reused_by_id(self):
        workbook = FakeWorkbook("existing", "Cell Reports", [FakeWorksheet(0, "Sheet1")])
        store = MemoryOperationStore()
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="existing")
        first_targets, _ = pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, None)
        first_ids = {role: target.worksheet_id for role, target in first_targets.items()}
        self.assertEqual(len(workbook.add_requests), len(ROLES))
        self.assertEqual(len(store.operations), len(ROLES))
        second_targets, _ = pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, None)
        self.assertEqual({role: target.worksheet_id for role, target in second_targets.items()}, first_ids)
        self.assertEqual(len(workbook.add_requests), len(ROLES))

    def test_populated_existing_alias_blocks_missing_tab_creation(self):
        title = cell_worksheet_title(ACCOUNT, ROLES[0], settings.CELL_FORMAT_NAMING_SCHEME)
        workbook = FakeWorkbook(
            "existing", "Cell Reports", [FakeWorksheet(900, title, [["unmanaged data"]])],
        )
        store = MemoryOperationStore()
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="existing")
        with self.assertRaisesRegex(ValueError, "unpublished v2 target is not empty"):
            pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, None)
        self.assertEqual(workbook.add_requests, [])
        self.assertEqual(store.operations, {})

    def test_worksheet_creation_flag_is_independent_from_workbook_creation_flag(self):
        workbook = FakeWorkbook("existing", "Cell Reports")
        store = MemoryOperationStore()
        config = _configuration(
            CELL_FORMAT_SPREADSHEET_ID="existing",
            CELL_FORMAT_CREATE_SPREADSHEET_IF_MISSING="false",
            CELL_FORMAT_CREATE_WORKSHEETS_IF_MISSING="false",
        )
        with self.assertRaisesRegex(FileNotFoundError, "worksheet creation is disabled"):
            pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, None)
        self.assertEqual(workbook.add_requests, [])
        self.assertEqual(store.operations, {})

    def test_cluster_cannot_fork_publication_history_to_another_workbook(self):
        workbook = FakeWorkbook("new-workbook", "Cell Reports")
        store = MemoryOperationStore(conflicting_targets={"old-workbook"})
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="new-workbook")
        with self.assertRaisesRegex(ValueError, "another workbook"):
            pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, None)
        self.assertEqual(workbook.add_requests, [])
        self.assertEqual(store.operations, {})

    def test_configured_missing_alias_tabs_use_the_pinned_sheet_ids(self):
        configured = {ACCOUNT: {role: 701 + index for index, role in enumerate(ROLES)}}
        config = _configuration(
            CELL_FORMAT_SPREADSHEET_ID="existing",
            CELL_FORMAT_WORKSHEET_IDS=json.dumps(configured),
        )
        workbook = FakeWorkbook("existing", "Cell Reports")
        targets, _ = pipeline._resolve_cell_targets(
            MemoryOperationStore(), workbook, CLUSTER, config, None,
        )
        self.assertEqual(
            {role: target.worksheet_id for role, target in targets.items()}, configured[ACCOUNT],
        )
        self.assertEqual([request["sheetId"] for request in workbook.add_requests], list(configured[ACCOUNT].values()))

    def test_prior_publication_with_missing_tab_fails_before_creation(self):
        workbook = FakeWorkbook("existing", "Cell Reports")
        role_snapshot = {}
        for index, role in enumerate(ROLES):
            title = cell_worksheet_title(ACCOUNT, role, settings.CELL_FORMAT_NAMING_SCHEME)
            worksheet_id = 800 + index
            role_snapshot[role] = {"worksheet_id": worksheet_id, "title": title}
            if role != ROLES[1]:
                workbook._worksheets.append(FakeWorksheet(worksheet_id, title))
        publication = {
            "status": "COMPLETE",
            "snapshot": {
                "schema_version": 2, "spreadsheet_id": workbook.id,
                "worksheets": role_snapshot,
            },
        }
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="existing")
        store = MemoryOperationStore()
        with self.assertRaisesRegex(ValueError, "committed cell publication worksheet"):
            pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, publication)
        self.assertEqual(workbook.add_requests, [])
        self.assertEqual(store.operations, {})

    def test_ambiguous_add_sheet_reconciles_exact_readback_but_never_retries(self):
        workbook = FakeWorkbook("existing", "Cell Reports")
        workbook.fail_add = "after"
        store = MemoryOperationStore()
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="existing")
        targets, _ = pipeline._resolve_cell_targets(store, workbook, CLUSTER, config, None)
        self.assertEqual(len(targets), len(ROLES))
        self.assertEqual(len(workbook.add_requests), len(ROLES))
        self.assertEqual(set(store.operations.values()), {"IDLE"})

        failed = FakeWorkbook("failed", "Cell Reports")
        failed.fail_add = "before"
        failed_store = MemoryOperationStore()
        failed_config = _configuration(CELL_FORMAT_SPREADSHEET_ID="failed")
        with self.assertRaisesRegex(RuntimeError, "blocked for operator reconciliation"):
            pipeline._resolve_cell_targets(failed_store, failed, CLUSTER, failed_config, None)
        self.assertEqual(len(failed.add_requests), 1)
        self.assertEqual(list(failed_store.operations.values()), ["UNKNOWN"])

    def test_existing_alias_tabs_are_reused_and_not_shared(self):
        worksheets = [
            FakeWorksheet(
                index + 900,
                cell_worksheet_title(ACCOUNT, role, settings.CELL_FORMAT_NAMING_SCHEME),
            )
            for index, role in enumerate(ROLES)
        ]
        workbook = FakeWorkbook(
            "existing", "Cell Reports", worksheets,
            [{"emailAddress": "writer@example.test", "role": "owner"}],
        )
        config = _configuration(CELL_FORMAT_SPREADSHEET_ID="existing")
        targets, _ = pipeline._resolve_cell_targets(
            MemoryOperationStore(), workbook, CLUSTER, config, None,
        )
        self.assertEqual(
            [targets[role].worksheet_id for role in ROLES],
            [worksheet.id for worksheet in worksheets],
        )
        self.assertEqual(workbook.add_requests, [])
        self.assertEqual(workbook.shares, [])

    def test_existing_workbook_access_requires_writer_and_all_finance_editors(self):
        workbook = FakeWorkbook(
            "existing", "Cell Reports",
            permissions=[{"emailAddress": "writer@example.test", "role": "owner"}],
        )
        with self.assertRaisesRegex(PermissionError, "sharing was not changed"):
            pipeline._verify_workbook_access(workbook, _configuration())
        self.assertEqual(workbook.shares, [])

    def test_drive_file_scope_is_added_only_for_creation_capability(self):
        with patch("google.oauth2.service_account.Credentials.from_service_account_info") as credentials, \
                patch("google.auth.transport.requests.AuthorizedSession"), \
                patch("requests.adapters.HTTPAdapter"), patch("gspread.Client"):
            make_cell_sheet_client({"client_email": "writer@example.test"})
            default_scopes = credentials.call_args.kwargs["scopes"]
            make_cell_sheet_client({"client_email": "writer@example.test"}, include_drive_file=True)
            create_scopes = credentials.call_args.kwargs["scopes"]
        drive_file = "https://www.googleapis.com/auth/drive.file"
        self.assertNotIn(drive_file, default_scopes)
        self.assertIn("https://www.googleapis.com/auth/spreadsheets", create_scopes)
        self.assertIn("https://www.googleapis.com/auth/drive.metadata.readonly", create_scopes)
        self.assertIn(drive_file, create_scopes)


if __name__ == "__main__":
    unittest.main()
