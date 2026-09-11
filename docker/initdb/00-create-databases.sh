#!/usr/bin/env bash
set -euo pipefail

# Project roles and databases are provisioned by finpay-db-provision after
# PostgreSQL is healthy. Init scripts run as the postgres user, while local
# Compose file-backed secrets are commonly readable only by the host owner.
# Keeping secret reads out of this phase makes clean-volume startup reliable.
cp /docker-entrypoint-initdb.d/01-pg-hba.conf /var/lib/postgresql/18/docker/pg_hba.conf
printf '%s\n' 'Custom pg_hba.conf installed; project databases deferred to finpay-db-provision'
