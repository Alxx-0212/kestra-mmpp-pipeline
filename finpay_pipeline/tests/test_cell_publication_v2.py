"""Offline contracts for stateless cell publication targets."""
import unittest

from finpay_pipeline.cell_format.settings import (
    CELL_FORMAT_NAMING_SCHEME, REPORTING_START_DATE, account_settings, report_config,
    sheet_configuration, sheet_targets,
)
from finpay_pipeline.cell_publication import publish_cell_cluster
from finpay_pipeline.cell_publication_v2 import (
    ROLES, cell_worksheet_title, preflight_cell_cluster_v2,
    publish_cell_cluster_v2, validate_output_targets,
)


BASE_ENV = {
    "CELL_FORMAT_SPREADSHEET_ID": "cell-workbook",
    "CELL_FORMAT_SPREADSHEET_NAME": "Cell Reports",
    "CELL_FORMAT_LEGACY_SPREADSHEET_ID": "legacy-workbook",
    "CELL_FORMAT_WRITER_EMAIL": "writer@example.test",
    "CELL_FORMAT_FINANCE_EMAILS": "finance@example.test",
}


class CellPublicationTargetTests(unittest.TestCase):
    def _targets(self, clusters=("412110", "422010"), *, duplicate_ids=False):
        configuration = sheet_configuration(BASE_ENV)
        accounts, targets = {}, {}
        next_id = 1000
        for cluster_id in clusters:
            account, _ = account_settings(cluster_id)
            accounts[account] = report_config(cluster_id, REPORTING_START_DATE)
            role_ids = {
                role: (1000 + index if duplicate_ids else next_id + index)
                for index, role in enumerate(ROLES)
            }
            targets[account] = sheet_targets(
                cluster_id, worksheet_ids=role_ids, configuration=configuration,
            )[2]
            next_id += len(ROLES)
        return configuration, accounts, targets

    def test_exact_titles_and_ids_are_unique_on_one_nonlegacy_workbook(self):
        configuration, accounts, targets = self._targets()
        validate_output_targets(
            configuration.spreadsheet_id, configuration.legacy_spreadsheet_id,
            accounts, targets,
        )
        for account, roles in targets.items():
            for role, target in roles.items():
                self.assertEqual(
                    target.title, cell_worksheet_title(account, role, CELL_FORMAT_NAMING_SCHEME),
                )
                self.assertEqual(target.spreadsheet_id, "cell-workbook")

    def test_target_validation_rejects_legacy_workbook_and_reused_ids(self):
        configuration, accounts, targets = self._targets()
        with self.assertRaisesRegex(ValueError, "distinct"):
            validate_output_targets(
                configuration.legacy_spreadsheet_id, configuration.legacy_spreadsheet_id,
                accounts, targets,
            )
        _, accounts, targets = self._targets(duplicate_ids=True)
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_output_targets("cell-workbook", "legacy-workbook", accounts, targets)

    def test_snapshot_status_publishers_are_retired(self):
        with self.assertRaisesRegex(RuntimeError, "stateful cell publication is retired"):
            publish_cell_cluster(None, None, None, None, None, None, changed_dates=())
        with self.assertRaisesRegex(RuntimeError, "stateful cell publication preflight is retired"):
            preflight_cell_cluster_v2(None, None, None, None, None)
        with self.assertRaisesRegex(RuntimeError, "stateful cell publication is retired"):
            publish_cell_cluster_v2(None, None, None, None, None, None, changed_dates=())


if __name__ == "__main__":
    unittest.main()
