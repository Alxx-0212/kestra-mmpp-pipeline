import unittest

from finpay_pipeline.tests.scale_database_guard import validate_scale_test_dsn


class ScaleDatabaseGuardTests(unittest.TestCase):
    def test_loopback_database_is_accepted(self):
        settings = validate_scale_test_dsn(
            "host=127.0.0.1 port=5432 dbname=finpay_cell_scale_test user=finpay_scale_test",
        )
        self.assertEqual(settings["host"], "127.0.0.1")

    def test_remote_hostaddr_is_rejected_even_with_loopback_host(self):
        dsn = (
            "host=127.0.0.1 hostaddr=192.0.2.10 port=5432 "
            "dbname=finpay_cell_scale_test user=finpay_scale_test"
        )
        with self.assertRaisesRegex(ValueError, "non-loopback PostgreSQL hostaddr"):
            validate_scale_test_dsn(dsn)

    def test_hostaddr_lists_must_all_be_loopback(self):
        dsn = (
            "host=localhost hostaddr=127.0.0.1,192.0.2.10 port=5432 "
            "dbname=finpay_cell_scale_test user=finpay_scale_test"
        )
        with self.assertRaisesRegex(ValueError, "non-loopback PostgreSQL hostaddr"):
            validate_scale_test_dsn(dsn)


if __name__ == "__main__":
    unittest.main()
