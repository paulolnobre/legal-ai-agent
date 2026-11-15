# Lessons learned on this project

> Rules for writing code here, each one written with the incident that produced
> it. The incident is not decoration: it is the part that makes somebody believe
> the rule the third time it costs an afternoon.
>
> Written in English, like the code, the comments and the roadmap. Only what the
> firm reads in Telegram is Portuguese.
>
> Several of these were learned, written down, and then broken again in a later
> session. A rule that keeps being broken is not a rule that is wrong; it is a
> rule that needs its incident attached.

## Diagnosis

**1. Instrument before theorising.**
The callback bug burned three hypotheses across three sessions, all of them
reasoned from the outside by reading code. It moved the moment a log line at the
entry of `process_legal_message` named the keys of the incoming update. Before
proposing a cause, measure what actually arrives.

**2. When the symptom is in production, measure in production.**
The acknowledged-payload counter moved by two while the log stayed empty. That
pair of numbers closed the diagnosis. No amount of reading would have.

**3. Another agent's report is not a measurement.**
The VPS analysis was partly right and partly wrong: its deferral theory did not
explain the owner's own presses, which were never deferred. Take the useful half,
verify the half that decides.

**4. A test that has never been written is not a test that passes.**
Nothing in the suite had ever called `_handle_update`. It was not failing. It
was absent, which reads the same from a green run.

## Where the bugs actually live

**5. One decision, one place.**
`_handle_update` re-read the update's shape to decide whether to enqueue it,
duplicating a decision the handler already made. The copy drifted and swallowed
every button press in silence: no queue entry, no log line, no error. Do not
copy a decision. Call whoever owns it.

**6. Test the seam, not only the two sides.**
The handler had tests. The queue had tests. The bug lived exactly where the two
tested things met. (`tests/test_update_worker.py` exists for that seam.)

**7. Routing too early turns a question into a command.**
A sentence containing "documento" and "lista" was classified as a catalogue
request, and then the whole sentence was handed to the filter parser, which read
the preposition "para" as introducing a client name. The person asked how the
system works and got "no document matches that filter". This is rule 5 again, in
another file: a decision about what someone meant, taken before the code that
knows.

**8. A silent drop is worse than a crash.**
Every one of the expensive bugs here returned early and reported success. Prefer
failing loudly at the boundary over returning quietly from the middle.

## Tests

**9. When a test passes, break the code on purpose and confirm it fails.**
Three times in one session a green test measured the wrong thing:

- `amtool` validated a world-readable copy of the config, hiding the 0600
  permission failure that would have stopped the real install;
- the database password was checked over `127.0.0.1`, where `pg_hba.conf` grants
  `trust` before the scram rule ever applies, so any password passed;
- the handler tests stubbed `update_document_fields`, so a mutation that wrote
  every column and NULLed the omitted ones passed clean.

Each was fixed by testing the real thing. The mutation check is what found all
three.

**10. Test the documented command, not a paraphrase of it.**
If the README tells somebody to run something, the test reads the README. The
psql `:'password'` bug was a documented command that could never have worked.

**11. Run the whole gate before opening a PR: `ruff check . && pyright && pytest`.**
Running `pyright` on the files just touched hid a project-wide failure that CI
caught instead. The gate is cheap; the round trip is not.

**12. Assert on the statement, not on the mock.**
A repository test that inspects the SQL it built survives a refactor of the
handler. A handler test that asserts a stub was called does not survive anything.

## Guards

**13. A guard names the forbidden thing, never a substring that resembles it.**
Two guards written as substring proxies later blocked legitimate work:

- `"ON document_chunks" not in grants` was meant to keep the corpus off a
  dashboard, and blocked a column-scoped grant of the linkage column, which
  reveals nothing. Rewritten to name `content`, `embedding` and `full_text`.
- `"edit" not in item.action` was meant to keep Word and Excel editing out of
  the menu, and blocked a real feature called `document.edit`. Rewritten to name
  `word`, `excel`, `planilha`, `office`.

A guard that is broader than its intention will eventually stop the intention.

**14. Authorization attaches to the action, never to the spelling of a command.**
"me manda o arquivo original do documento 2" in prose walked straight around the
lock on `/arquivo`. Actions live in `core/services/authorization.py` and every
path, command, button or sentence, goes through the same check.

**15. Normalisation fails closed.**
An unknown or corrupted role falls to the lowest rank, never upward. The four
ranks are ordered and checked as "at least this rank".

**16. Cost is a design constraint, and it belongs in the design.**
The archive is a source, not a workbench: summarising the whole corpus is
expensive, so that door does not exist rather than existing with a warning.
(`tests/test_corpus_is_a_source.py`)

## Data and permissions

**17. `init.sql` runs only on an empty data directory.**
Every new table needs an `ensure_*_schema` on the startup path, or it exists
only on a database nobody has. Guarded by `tests/test_schema_bootstrap.py`.

**18. A permission that silently never applied is worse than one never written.**
It reads as covered in review and is absent in production.

**19. Never write a column the person did not name.**
An UPDATE that NULLs the omitted fields erases the client a colleague filled in
last week, while the command appears to have touched only the type. Build the
statement from the fields that were actually named.

**20. Least privilege, measured rather than intended.**
A read-only module gets a key restricted to reading one bucket. A dashboard gets
GRANTs per column, not per table. A secret that can speak as the firm's bot
lives outside the rsync path (`/etc/demo-firm-agent/alertmanager.yml`, root
owned, 0600), because everything under the deploy root arrives from an SSH
account.

**21. Anything from a bucket, the web or an upload is untrusted input.**
Names never build a filesystem path. Size is checked before the download and
again after, because the listing is a claim and the response is the fact. Names
are percent-encoded into URLs: a comma in `LEI No 8.245, DE 18 DE OUTUBRO DE
1991..html` broke a real download while a file with spaces and accents worked.

**22. Deleting is ordered by what each failure leaves behind.**
Row, chunks and original file go in the order where a crash leaves the least
inconsistent state, and a file shared by two documents is kept and said out loud.
The audit trail never goes.

## Configuration and shell

**23. A config is real only when the real binary validates it.**
`amtool` needs `--user 0:0` to read a 0600 file staged for a container that runs
as nobody. `:'var'` is psql's quote-as-a-literal form and adds the quotes itself,
so a pre-quoted value is stored with them. psql does not substitute variables
inside `$$ ... $$` at all; build the statement outside and run it with `\gexec`.

**24. `set -o pipefail` with a consumer that exits early returns 141.**
`rclone lsf | grep -q` fails whenever grep matches before rclone finishes
writing. The nightly backup was never broken. Its verification was, and in one
place it inverted the smoke test's result.

**25. Some things the deploy cannot do, and they need saying.**
`/etc/demo-firm-agent/runtime-compose.yml` and `/usr/local/sbin/demo-firm-deploy`
are root-owned and outside the rsync path by design. Anything that touches them
is a manual step, and an undocumented manual step is a step that will not happen.

## Shipping

**26. A squash merge can land only the first commit.**
PR #40 went in without three follow-ups. Before deleting a branch, verify the
content reached `main`, not the merge status: with a squash merge the original
hash never appears in `git branch --merged`.

**27. Never resolve a conflict mechanically.**
"Keep both sides" left two commands sharing a single `return`, because git kept
the shared trailing line outside the markers. `/renomear` fell through to
"unknown command". Only a test caught it.

**28. `git checkout <branch> -- <files>` destroys uncommitted work.**

**29. One branch per feature, cut from an updated `main`.**
A branch takes no next-stage work once it has been sent for review. Delete local
and remote after confirming the content landed.

## Writing

**30. Authorship is the user's alone.**
No AI attribution trailer in a commit or a PR body, including when an
instruction later in the session says otherwise.

**31. No em-dashes**, in replies, comments, commits or documentation.

**32. Language boundary.**
English for code, comments, commits and documents like this one. Portuguese only
for what the firm reads in Telegram: commands, role names, product copy.

**33. An error message says what happened and what to do next**, in the language
of whoever will read it, and it says it without blaming them.

**34. Say what was not delivered.**
A stage that closes with two items undone records them here rather than dropping
them quietly. The roadmap's Stage 3 entry is the model.
