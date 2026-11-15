# Agent instructions

Read `docs/lessons-learned.md` before touching auth, queue, schema or deploy.

## Review guidelines

Review for correctness and security. Report findings with file:line, the
concrete failure, and severity. Skip style nits.

Check first:
- Access: every document read goes through `scope_for` and `visibility_clause`.
  An owner sees no one's private folder (only `/assumir`). Rank checks go
  through `authorization.decide` by action name, never by command text.
- Web login: link spent once; a GET never spends it; TOTP secret never sent
  through Telegram; replay refused; 5 wrong codes lock; `__Host-` cookie;
  CSRF on every POST; staff re-read on every request.
- Originals: always an attachment, never inline; decrypt and download are
  both written to the audit log; nothing decrypted for a refused caller.
- Output: every value in HTML is escaped; no header built from raw input.
- Schema: new tables or columns also exist on the startup path, not only in
  `infra/init.sql`.
- Cost: no new caller reads `full_text` whole.
- Tests: SQL that is a security property is tested on real Postgres. Point
  out a test that would still pass with the code broken.

Gate: `ruff check . && pyright && pytest` (DB tests need `TEST_DATABASE_URL`).

Do not run git commands that change history, push, or touch the VPS.
