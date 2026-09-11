#!/usr/bin/env bash
set -euo pipefail

required_env=(
  KESTRA_POSTGRES_DB KESTRA_POSTGRES_USER
  FINPAY_DB_NAME FINPAY_DB_USER
  FINPAY_TXN_DB_NAME FINPAY_TXN_DB_USER
  LINKAJA_DB_NAME LINKAJA_DB_USER
  SUPERSET_DB_NAME SUPERSET_DB_USER
  GRIST_DB_NAME GRIST_DB_USER
)
for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    printf 'Required database setting is empty: %s\n' "$name" >&2
    exit 1
  fi
done

read_secret() {
  local path="$1"
  if [[ ! -r "$path" ]]; then
    printf 'Database secret file is not readable: %s\n' "$path" >&2
    exit 1
  fi
  tr -d '\r\n' < "$path"
}

admin_password="$(read_secret /run/secrets/kestra_postgres_password)"
readonly_password="$(read_secret /run/secrets/finpay_readonly_password)"

export PGHOST=postgres
export PGPORT=5432
export PGUSER="$KESTRA_POSTGRES_USER"
export PGPASSWORD="$admin_password"

psql_admin() {
  psql -v ON_ERROR_STOP=1 --dbname="$KESTRA_POSTGRES_DB" "$@"
}

provision_role() {
  local role_name="$1"
  local password="$2"
  psql_admin \
    --set=provision_role="$role_name" \
    --set=provision_password="$password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'provision_role', :'provision_password')
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'provision_role'
)\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'provision_role', :'provision_password')\gexec
SQL
}

provision_database() {
  local database_name="$1"
  local owner_name="$2"
  local password="$3"
  provision_role "$owner_name" "$password"
  psql_admin \
    --set=provision_database="$database_name" \
    --set=provision_owner="$owner_name" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'provision_database', :'provision_owner')
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = :'provision_database'
)\gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'provision_database', :'provision_owner')\gexec
SQL
  psql -v ON_ERROR_STOP=1 --dbname="$database_name" \
    --set=provision_owner="$owner_name" <<'SQL'
SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', current_database(), :'provision_owner')\gexec
SQL
}

provision_database "$FINPAY_DB_NAME" "$FINPAY_DB_USER" "$(read_secret /run/secrets/finpay_db_password)"
provision_database "$FINPAY_TXN_DB_NAME" "$FINPAY_TXN_DB_USER" "$(read_secret /run/secrets/finpay_txn_db_password)"
provision_database "$LINKAJA_DB_NAME" "$LINKAJA_DB_USER" "$(read_secret /run/secrets/linkaja_db_password)"
provision_database "$SUPERSET_DB_NAME" "$SUPERSET_DB_USER" "$(read_secret /run/secrets/superset_db_password)"
provision_database "$GRIST_DB_NAME" "$GRIST_DB_USER" "$(read_secret /run/secrets/grist_db_password)"

provision_role finpay_readonly "$readonly_password"
psql -v ON_ERROR_STOP=1 --dbname="$FINPAY_DB_NAME" <<'SQL'
GRANT CONNECT ON DATABASE finpay TO finpay_readonly;
GRANT USAGE ON SCHEMA public TO finpay_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO finpay_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO finpay_readonly;
SQL

printf 'event=mmpp_database_provision status=success databases=5\n'
