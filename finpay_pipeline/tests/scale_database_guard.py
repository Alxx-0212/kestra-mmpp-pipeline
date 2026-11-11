"""Shared fail-closed target guard for opt-in scale database tools."""
from __future__ import annotations

import ipaddress

from psycopg.conninfo import conninfo_to_dict

DATABASE_GUARD = "finpay_cell_scale_test"
USER_GUARD = "finpay_scale_test"


def validate_scale_test_dsn(dsn: str) -> dict:
    settings = conninfo_to_dict(dsn)
    if (settings.get("dbname") != DATABASE_GUARD
            or settings.get("user") != USER_GUARD
            or settings.get("host") not in ("127.0.0.1", "localhost")
            or settings.get("port") != "5432"):
        raise ValueError("scale test refused an unapproved database identity or non-loopback target")
    hostaddr = settings.get("hostaddr")
    if hostaddr:
        addresses = [address.strip() for address in hostaddr.split(",")]
        try:
            loopback = all(ipaddress.ip_address(address).is_loopback for address in addresses)
        except ValueError:
            loopback = False
        if not addresses or not loopback:
            raise ValueError("scale test refused a non-loopback PostgreSQL hostaddr")
    return settings
