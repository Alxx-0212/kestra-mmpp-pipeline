import os
import unittest


def _dsn():
    return os.environ.get("FINPAY_TOPUP_TEST_DSN", "")


def _has_dsn():
    return bool(_dsn())


def _conn():
    import psycopg
    return psycopg.connect(_dsn())


class RequiresPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _has_dsn():
            raise unittest.SkipTest("FINPAY_TOPUP_TEST_DSN not set")
