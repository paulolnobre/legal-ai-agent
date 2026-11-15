-- A database role for Grafana that can only read, and only what a dashboard
-- has a reason to show.
--
-- Run once against the deployed database, as the application's own role:
--
--   docker compose -p demo-firm-v2 \
--     --env-file /opt/demo-firm-agent-v2/.env \
--     -f /etc/demo-firm-agent/runtime-compose.yml \
--     exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     -v password="$GRAFANA_DB_PASSWORD" \
--     < /opt/demo-firm-agent-v2/infra/sql/grafana_readonly.sql
--
-- The value is passed raw. `:'password'` below is psql's quote-as-a-literal
-- form, so it adds the quotes itself: a value that arrives already quoted is
-- stored WITH those quotes, and the role then has a password nothing else
-- knows. Measured, because the mistake is silent: passing 'senha' set a
-- password that only 'senha' with the quotes could log in with, while Grafana
-- read GRAFANA_DB_PASSWORD from the environment and was refused. The shell
-- quotes above are the shell's, and do not reach psql.
--
-- This file is not part of init.sql on purpose. init.sql runs only when the
-- data directory is empty, so on any database that already exists it would
-- never execute, and a permission that silently never applied is worse than
-- one that was never written.
--
-- The grants are per table and, where a table holds the firm's material, per
-- column. Two rules decide what is left out:
--
--   Client content never leaves the application. document_chunks holds the
--   corpus and its embeddings, and documents.full_text holds whole documents.
--   A dashboard counts them; it does not read them.
--
--   Personal data is granted only where the dashboard's question needs it.
--   Counting lawyers by role needs the role, not the email or the chat id.

\set ON_ERROR_STOP on

-- CREATE when it is missing, ALTER when it already exists, so running this a
-- second time rotates the password instead of failing.
--
-- The statement is built and then executed with \gexec rather than wrapped in
-- a DO block. psql does not substitute :'password' inside dollar quoting, so
-- the block version parses the literal text ":'password'" and dies on it.
SELECT format(
  '%s ROLE grafana_ro LOGIN PASSWORD %L',
  CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro')
       THEN 'ALTER' ELSE 'CREATE' END,
  :'password'
) \gexec

-- Reading requires reaching the database and the schema first, and nothing
-- beyond reading. The database is named at run time so this file does not have
-- to know whether it is running against demo_firm or demo_firm_v2.
SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_ro', current_database()) \gexec

GRANT USAGE ON SCHEMA public TO grafana_ro;

-- The catalogue, without the documents themselves.
GRANT SELECT (
  id, title, doc_type, matter, source_filename, source_size_bytes,
  uploaded_by, created_at
) ON documents TO grafana_ro;

-- Who the firm is, for counting by role. Not who they are personally.
GRANT SELECT (id, name, role, active, created_at) ON staff TO grafana_ro;

-- The audit trail. This is the point of having the datasource at all: who
-- asked for what, and when. The query text is included because an audit that
-- cannot say what was asked is not an audit.
GRANT SELECT ON document_access_log TO grafana_ro;
GRANT SELECT ON staff_management_log TO grafana_ro;

-- Access requests, so a pending one cannot sit unnoticed for a week.
GRANT SELECT (
  id, request_code, name, username, status, decided_by, requested_at, decided_at
) ON access_requests TO grafana_ro;

-- Flows in progress, to see steps and buttons that expire without an answer.
-- Neither the target of a pending step nor a button's result is a dashboard's
-- business.
GRANT SELECT (chat_id, kind, action, created_at, expires_at)
  ON pending_actions TO grafana_ro;
GRANT SELECT (
  chat_id, staff_id, issued_role, action, operation, state,
  created_at, expires_at, used_at
) ON callback_actions TO grafana_ro;

-- Counting passages without reading one. The dashboard answers "how much of
-- this document is indexed", which needs the linkage and nothing else: content
-- and embedding stay unreadable, so the corpus cannot be reconstructed from a
-- panel. Granting the column rather than the table is what makes that true.
GRANT SELECT (document_id) ON document_chunks TO grafana_ro;

-- What each month cost. Numbers the firm pays for, with nothing of a client
-- in them, so the whole table is readable and the cost panel needs no
-- exception anywhere else.
GRANT SELECT ON budget_history TO grafana_ro;

-- Anything added later must be granted deliberately, one table at a time.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM grafana_ro;
