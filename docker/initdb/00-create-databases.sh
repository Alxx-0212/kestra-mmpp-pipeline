#!/bin/bash
set -e

# This script runs during PostgreSQL container initialization.
# It creates the project databases and roles for the MMPP pipeline.
# Network/DB naming follows the mmpp-finance-network contract:
#   - finpay:     existing FinPay Top Up + reconciliation database
#   - finpay_txn: new FinPay Transaction processing database (separate from top-up)
#   - superset:   Superset metadata database (Stage 1: metadata DB)
#   - linkaja:    LinkAja fee pipeline data (future)
#   - grist:      Grist metadata database

# First, install custom pg_hba.conf for Docker network authentication
cp /docker-entrypoint-initdb.d/01-pg-hba.conf /var/lib/postgresql/18/docker/pg_hba.conf
echo "Custom pg_hba.conf installed"

# Helper function to create role with md5 password encryption (must be in same transaction)
create_role() {
    local role_name="$1"
    local password="$2"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "SET password_encryption = 'md5'; CREATE ROLE $role_name LOGIN PASSWORD '$password';"
}

# Helper function to create database (must be separate transaction)
create_db() {
    local db_name="$1"
    local owner="$2"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "CREATE DATABASE $db_name OWNER $owner;"
}

# Helper function to grant privileges
grant_privs() {
    local db_name="$1"
    local role_name="$2"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "GRANT ALL PRIVILEGES ON DATABASE $db_name TO $role_name;"
}

# Create finpay database and role (existing contract DB)
create_role finpay "${FINPAY_DB_PASSWORD}"
create_db finpay finpay
grant_privs finpay finpay

# Create superset metadata database and role (Stage 1: metadata DB)
create_role superset "${SUPERSET_DB_PASSWORD}"
create_db superset superset
grant_privs superset superset

# Create finpay_txn database and role (new, separate from top-up)
create_role finpay_txn "${FINPAY_TXN_DB_PASSWORD}"
create_db finpay_txn finpay_txn
grant_privs finpay_txn finpay_txn

# Create linkaja database and role
create_role linkaja "${LINKAJA_DB_PASSWORD}"
create_db linkaja linkaja
grant_privs linkaja linkaja

# Create grist database and role
create_role grist "${GRIST_DB_PASSWORD}"
create_db grist grist
grant_privs grist grist

# Create finpay_readonly user for Grist to connect to finpay database
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "SET password_encryption = 'md5'; CREATE ROLE finpay_readonly LOGIN PASSWORD '${FINPAY_READONLY_PASSWORD}';"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "finpay" -c "GRANT CONNECT ON DATABASE finpay TO finpay_readonly; GRANT USAGE ON SCHEMA public TO finpay_readonly; GRANT SELECT ON ALL TABLES IN SCHEMA public TO finpay_readonly; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO finpay_readonly;"

echo "Database initialization complete"
