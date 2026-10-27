"""Bounded retries for read-only cell Sheet adapter calls."""
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from requests.exceptions import ConnectionError, Timeout

from finpay_pipeline.cell_publication import ExistingSheetAdapter, _padded
from finpay_pipeline.cell_workflow import make_cell_sheet_client


class FakeResponse:
    def __init__(self, status_code=200, *, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = payload

    def json(self):
        return self.payload


class APIReadError(Exception):
    def __init__(self, status_code, *, headers=None):
        self.response = FakeResponse(status_code, headers=headers)


class CellSheetReadRetriesTests(unittest.TestCase):
    def setUp(self):
        self.spreadsheet = SimpleNamespace(
            id="approved-book",
            client=SimpleNamespace(request=Mock()),
            fetch_sheet_metadata=Mock(),
            batch_update=Mock(),
        )
        self.worksheet = SimpleNamespace(id=7, title="Approved Tab", get=Mock())
        self.adapter = ExistingSheetAdapter(self.spreadsheet, self.worksheet)

    def test_sheet_client_allows_slow_responses_without_retrying_writes(self):
        credentials = Mock()
        session = Mock()
        client = Mock()
        adapter = Mock()

        with patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=credentials), \
             patch("google.auth.transport.requests.AuthorizedSession", return_value=session), \
             patch("requests.adapters.HTTPAdapter", return_value=adapter) as http_adapter, \
             patch("gspread.Client", return_value=client) as gspread_client:
            result = make_cell_sheet_client({"client_email": "writer@example.test"})

        self.assertIs(result, client)
        session.mount.assert_called_once_with("https://", adapter)
        http_adapter.assert_called_once_with(max_retries=0)
        gspread_client.assert_called_once_with(auth=credentials, session=session)
        client.set_timeout.assert_called_once_with((10, 120))

    def test_value_read_retries_timeouts_connections_and_transient_statuses(self):
        self.worksheet.get.side_effect = [
            Timeout("synthetic timeout"),
            ConnectionError("synthetic connection failure"),
            APIReadError(500),
            [["value"]],
        ]

        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            self.assertEqual(self.adapter.read(1, 1), [["value"]])

        self.assertEqual(self.worksheet.get.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8)])

    def test_read_exhaustion_stops_after_six_attempts(self):
        errors = [APIReadError(429) for _ in range(6)]
        self.worksheet.get.side_effect = errors

        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            with self.assertRaises(APIReadError) as raised:
                self.adapter.read(1, 1)

        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(self.worksheet.get.call_count, 6)
        self.assertEqual(sleep.call_args_list, [call(2), call(4), call(8), call(16), call(32)])

    def test_request_timeout_status_and_drive_quota_errors_are_read_only_retried(self):
        quota = APIReadError(403)
        quota.error = {"errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded"}]}
        self.worksheet.get.side_effect = [APIReadError(408), quota, [["value"]]]
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            self.assertEqual(self.adapter.read(1, 1), [["value"]])
        self.assertEqual(self.worksheet.get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(4)])

    def test_non_rate_limit_drive_403_and_quota_write_errors_are_never_retried(self):
        for domain, reason in (("global", "forbidden"), ("usageLimits", "accessNotConfigured"),
                               ("global", "rateLimitExceeded"), ("usageLimits", "dailyLimitExceeded")):
            with self.subTest(domain=domain, reason=reason):
                self.worksheet.get.reset_mock(side_effect=True)
                error = APIReadError(403)
                error.error = {"errors": [{"domain": domain, "reason": reason}]}
                self.worksheet.get.side_effect = error
                with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
                    with self.assertRaises(APIReadError):
                        self.adapter.read(1, 1)
                self.assertEqual(self.worksheet.get.call_count, 1)
                sleep.assert_not_called()
        error = APIReadError(403)
        error.error = {"errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}
        self.spreadsheet.batch_update.side_effect = error
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            with self.assertRaises(APIReadError):
                self.adapter.batch_update([{"updateCells": {}}])
        self.spreadsheet.batch_update.assert_called_once()
        sleep.assert_not_called()

    def test_owner_and_sheet_metadata_reads_retry_independently(self):
        self.spreadsheet.client.request.side_effect = [
            APIReadError(503, headers={"Retry-After": "3.5"}),
            FakeResponse(payload={"id": "approved-book", "owners": [{"emailAddress": "owner@example.test"}]}),
        ]
        self.spreadsheet.fetch_sheet_metadata.side_effect = [
            APIReadError(429),
            {"sheets": [{"properties": {"sheetId": 7, "title": "Approved Tab"}}]},
        ]

        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            metadata = self.adapter.metadata()

        self.assertEqual(metadata["implicitOwnerEmails"], ["owner@example.test"])
        self.assertEqual(self.spreadsheet.client.request.call_count, 2)
        self.assertEqual(self.spreadsheet.client.request.call_args.args[0], "get")
        self.assertEqual(self.spreadsheet.fetch_sheet_metadata.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(3.5), call(2)])

    def test_style_structure_and_each_page_are_retried(self):
        attempts = Counter()
        requests = []

        def fetch_metadata(*, params):
            key = params.get("ranges", "structure")
            requests.append(key)
            attempts[key] += 1
            if attempts[key] == 1:
                raise APIReadError(503)
            if key == "structure":
                return {"spreadsheetId": "approved-book", "sheets": [{
                    "properties": {"sheetId": 7}, "merges": [], "bandedRanges": [],
                }]}
            start = 0 if "A1:A100" in key else 100
            return {
                "spreadsheetId": "approved-book",
                "properties": {"defaultFormat": {}},
                "sheets": [{
                    "properties": {"sheetId": 7, "gridProperties": {}},
                    "data": [{"startRow": start, "startColumn": 0}],
                }],
            }

        self.spreadsheet.fetch_sheet_metadata.side_effect = fetch_metadata
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            pages = list(self.adapter.read_style(101, 1))

        self.assertEqual([len(page["formats"]) for page in pages], [100, 1])
        self.assertEqual(requests, [
            "structure", "structure",
            "'Approved Tab'!A1:A100", "'Approved Tab'!A1:A100",
            "'Approved Tab'!A101:A101", "'Approved Tab'!A101:A101",
        ])
        self.assertEqual(sleep.call_args_list, [call(2), call(2), call(2)])

    def test_retry_after_is_honored_only_when_finite_and_within_cap(self):
        self.worksheet.get.side_effect = [
            APIReadError(429, headers={"retry-after": "5.5"}),
            [["value"]],
        ]
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            self.assertEqual(self.adapter.read(1, 1), [["value"]])
        self.assertEqual(sleep.call_args_list, [call(5.5)])

        for value in ("60.1", "NaN", "inf", "-1", "not-numeric", "Wed, 21 Oct 2026 07:28:00 GMT"):
            with self.subTest(retry_after=value):
                adapter = ExistingSheetAdapter(self.spreadsheet, self.worksheet)
                self.worksheet.get.reset_mock(side_effect=True)
                error = APIReadError(429, headers={"Retry-After": value})
                self.worksheet.get.side_effect = error
                with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
                    with self.assertRaises(APIReadError) as raised:
                        adapter.read(1, 1)
                self.assertIs(raised.exception, error)
                self.assertEqual(self.worksheet.get.call_count, 1)
                sleep.assert_not_called()

    def test_validation_and_permanent_http_errors_are_not_retried(self):
        for status_code in (400, 401, 403, 404):
            with self.subTest(status_code=status_code):
                adapter = ExistingSheetAdapter(self.spreadsheet, self.worksheet)
                self.worksheet.get.reset_mock(side_effect=True)
                error = APIReadError(status_code)
                self.worksheet.get.side_effect = error
                with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
                    with self.assertRaises(APIReadError) as raised:
                        adapter.read(1, 1)
                self.assertIs(raised.exception, error)
                self.assertEqual(self.worksheet.get.call_count, 1)
                sleep.assert_not_called()

        self.spreadsheet.client.request.reset_mock(side_effect=True)
        self.spreadsheet.client.request.return_value = FakeResponse(payload={"id": "wrong-book"})
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "ownership metadata identity mismatch"):
                self.adapter.metadata()
        self.assertEqual(self.spreadsheet.client.request.call_count, 1)
        self.spreadsheet.fetch_sheet_metadata.assert_not_called()
        sleep.assert_not_called()

        self.spreadsheet.fetch_sheet_metadata.reset_mock(side_effect=True)
        self.spreadsheet.fetch_sheet_metadata.return_value = {"spreadsheetId": "wrong-book"}
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "style spreadsheet identity mismatch"):
                list(self.adapter.read_style(1, 1))
        self.assertEqual(self.spreadsheet.fetch_sheet_metadata.call_count, 1)
        sleep.assert_not_called()

        self.worksheet.get.reset_mock(side_effect=True)
        self.worksheet.get.return_value = [["one"], ["unexpected"]]
        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "unexpected spreadsheet readback dimensions"):
                _padded(self.adapter.read(1, 1), 1, 1)
        self.assertEqual(self.worksheet.get.call_count, 1)
        sleep.assert_not_called()

    def test_write_calls_are_never_retried(self):
        error = APIReadError(503)
        self.spreadsheet.batch_update.side_effect = error

        with patch("finpay_pipeline.cell_publication.time.sleep") as sleep:
            with self.assertRaises(APIReadError) as raised:
                self.adapter.batch_update([{"updateCells": {}}])

        self.assertIs(raised.exception, error)
        self.spreadsheet.batch_update.assert_called_once_with({"requests": [{"updateCells": {}}]})
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
