# Observability

The application separates three concerns that have different retention and
privacy requirements:

- PostgreSQL `document_access_log` is the authoritative legal audit trail.
- LangSmith traces agent execution and model usage, with legal content hidden
  by default.
- Prometheus, Grafana, and JSON stdout logs cover application and infrastructure
  operations.

## LangSmith

Tracing is disabled by default. To enable metadata-only tracing, set:

```text
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<workspace API key>
LANGSMITH_PROJECT=legal-ai-agent
LANGSMITH_HIDE_INPUTS=true
LANGSMITH_HIDE_OUTPUTS=true
LANGSMITH_CAPTURE_CONTENT=false
```

The application refuses to start if input or output hiding is disabled without
also setting `LANGSMITH_CAPTURE_CONTENT=true`. Keep content capture disabled in
production unless the firm's privacy review explicitly approves the provider,
region, retention, access controls, and affected matters.

Safe trace metadata includes environment, application release, correlation ID,
provider, model, timing, token counts, result counts, and redacted input/output
sizes. It does not intentionally include names, Telegram chat IDs, questions,
answers, document text, or process numbers.

## Prometheus and Grafana

Start the optional local observability profile with the rest of the stack:

```bash
docker compose --profile observability up -d --build
```

Locally, Grafana binds to `127.0.0.1:3000` and Prometheus to `127.0.0.1:9090`.

On the VPS, Grafana is at `https://<LEGAL_WEB_HOST>/grafana/`, opened by the
pages' sign-in and only for the owner (`observability.view` in
`core/services/authorization.py`). The chain, each link asserted in
`tests/test_grafana_access.py`:

1. Traefik's `demo-firm-grafana-drop-user` middleware removes any
   `X-WEBAUTH-USER` the browser sent.
2. `demo-firm-grafana-auth` (forwardAuth) asks the application at
   `/internal/grafana/auth`, which reads the `__Host-session` cookie and the
   staff row like every page, and answers 204 with `X-WEBAUTH-USER: staff-<id>`
   for the owner, 403 for any other rank, 401 (or a redirect to `/web/` for a
   page load) for nobody. That path has no public route.
3. Traefik copies the header onto the request; Grafana runs in auth-proxy mode,
   with the login form, basic auth and anonymous access off, and accepts the
   header only from `10.203.17.0/28`, the `demo-firm-grafana` network that
   only Traefik and Grafana join. No port is published.

Prometheus has no authentication, so it stays on loopback, reached through an
SSH tunnel:

```bash
ssh -L 9090:127.0.0.1:9090 root@<vps>
```

Its console lists the rules in `infra/prometheus/alerts.yml` under Alerts. The
provisioned dashboard covers graph volume and p95 latency, LLM calls and tokens,
external integrations, dependency readiness, and all four Redis queue depths.

### Turning it on (once, as root)

Both compose files live outside the rsync path (lesson 25), so the deploy
cannot do this:

1. Install `infra/edge/edge-compose.yml` as
   `/etc/demo-firm-agent/edge-compose.yml` and bring the edge up again; it
   creates `demo-firm-grafana` with its subnet and joins Traefik to it. If
   `10.203.17.0/28` is taken on the host, change it in both compose files
   together.
2. Install `infra/vps/runtime-compose.yml` as
   `/etc/demo-firm-agent/runtime-compose.yml`.
3. Deploy. Grafana comes back with the new routes and `legal-bot` with the
   alias Traefik asks (`demo-firm-legal-bot`).

Grafana creates the user `staff-<id>` on the first visit. Dashboards saved by
the old `admin` login stay where they were.

Prometheus retains 180 days of metrics in the `prometheus_data` volume. Grafana
state is stored in `grafana_data`. Include both volumes in the VPS disk-capacity
monitoring plan; metrics are operational data, not the legal audit record.

### Costs

The "Costs (private)" dashboard shows what the agent costs, in US$ at public
list prices. Grafana opens only for the owner, through the pages' sign-in
above, so the figures are the owner's alone.

- Models: `legal_llm_tokens_total` per model, priced at the boxes at the top of
  the dashboard (gpt-4o and gpt-4o-mini). A model with no box appears in the
  "Tokens by model" table and is not in the total.
- Transcription: `legal_audio_seconds_total`, minutes of audio sent to Whisper (web and Telegram) at the Whisper price.
- Web search: Serper calls from `legal_external_calls_total`.
- Fixed: VPS and bucket storage, typed into the boxes (storage sizes start at 0).
- The month-by-month table reads `budget_history`, a worst-case figure kept in
  the database past Prometheus's 180 days.

Counters reset on a deploy; the panels use `increase()`, which carries across a
reset. Not counted: embeddings, Exa, Telegram, the domain, taxes and exchange fees.

The `/metrics` route must remain private. The public reverse proxy should route
only the Telegram webhook and, if desired, the minimal `/health` endpoint.

## Readiness by capability

`/health` is a minimal liveness endpoint. `/ready` checks Redis and PostgreSQL,
the dependencies required to accept and preserve Telegram work. It also returns
a separate `capabilities.external_research` object whose status is:

- `configured` when the selected web-search provider, DataJud, and official-page
  reading have valid local configuration;
- `degraded` when at least one external source can be used but another is
  missing or disabled;
- `unavailable` when neither the selected web-search provider nor DataJud has
  usable configuration.

The web-search dependency is reported as `web_search`, with a separate
`provider` field containing `serper` or `exa`. Only the credential and HTTPS
endpoint of the selected provider affect that dependency's status; the other
provider may remain unconfigured. The shared official-domain allowlist is still
validated whichever provider is selected.

The capability check is configuration-only. It exposes state and safe reason
codes, never keys, endpoint values, client data, or legal queries. External
degradation does not turn `/ready` into HTTP 503; only Redis or PostgreSQL
failure does that.

Run the live external canaries explicitly after changing credentials, source
policy, DNS, or egress rules:

```bash
python -m core.external_canary
```

One invocation makes at most one request each to the selected web-search
provider, DataJud, and an allowlisted official page. The search request may
consume one unit of that provider's API quota. The inputs are code-owned
constants: a generic Constitution query, a synthetic CNJ-shaped process number,
and a fixed or operator-authorized official URL. The command prints only
pass/fail, exception class, and latency metadata, and exits non-zero unless all
three probes pass. Do not schedule this command at the `/ready` polling interval;
choose a low-frequency external monitor only after its quota and alert
destination are approved.

## Original documents

Original uploads are stored in the private `document_storage` Docker volume,
not in PostgreSQL and never behind a public URL. An authorized Telegram staff
member can run `/docs` and then `/arquivo <id>` to receive an original. Each
delivery creates a `download_original` entry in `document_access_log`.

Back up this volume together with `postgres_data`; restoring PostgreSQL without
the matching originals loses retrieval of the source file, while restoring the
volume without PostgreSQL loses its metadata and audit trail.

## Alert rules

Prometheus loads rules for:

- the application not answering a scrape for two minutes;
- a non-empty dead-letter queue;
- Redis or PostgreSQL readiness failure;
- a graph error ratio above 10% for five minutes;
- an active queue that stops draining;
- a deferred queue that still holds work during office hours;
- an update held in processing for ten minutes;
- the pilot model budget at half, four fifths and all of its monthly ceiling.

The budget alerts read one gauge, `legal_budget_used_ratio`, which the
application publishes as a share of the configured ceiling rather than as a
count. That is why three thresholds need no three counters, and why changing a
ceiling in the configuration moves all three at once. The gauge carries only
the window (day or month) and whether it is input or output, so no alert about
spending can name a question, a client or a document.

The last of the three is not a warning about the future: at that point the
assistant is already refusing questions that need the model, answering with the
ceiling and its reopening window, while everything that needs no model keeps
working.

Redis has its own group, because the application counts what it puts in the
queue and not what the queue is made of:

- Redis unreachable;
- memory above 85% of its ceiling;
- a failing append-only write or background save;
- refused connections.

The memory rule matters more here than the number suggests. This Redis runs
`maxmemory-policy noeviction` on purpose, because dropping a lawyer's message to
free space is not a trade this application may make. The price of that choice is
that the ceiling is a wall: at 256MB every enqueue starts failing, the webhook
answers 503, and Telegram retries in a loop. Nothing is lost and nothing is
served, which is a hard state to recognise from the application's own metrics,
where it looks like silence.

The metric names come from `oliver006/redis_exporter`, checked against a running
exporter rather than from memory: `redis_up`, `redis_memory_used_bytes`,
`redis_memory_max_bytes`, `redis_aof_last_write_status`,
`redis_rdb_last_bgsave_status`, `redis_rejected_connections_total`,
`redis_connected_clients`, `redis_db_keys`.

The first of those exists because of a specific morning. On 2026-09-09 the bot
was down from 12:47 UTC and the only notice was a red deploy log that a person
happened to read. Everything else on this list is a symptom; that one is the
outage.

## Where an alert goes

Alertmanager delivers to Telegram, because that is where this firm already
looks. It talks to Telegram's servers directly rather than through the
assistant, which matters: the token keeps working while the bot process is
down, which is exactly when the first alert fires.

Alertmanager does not expand environment variables in its configuration and the
file therefore holds a token, so it is rendered on the host rather than
synchronized with the source:

```bash
# on the VPS, as root, once and after any change to the token or chat
sudo /opt/demo-firm-agent-v2/scripts/install_alertmanager_config.sh
```

It reads `ALERT_TELEGRAM_CHAT_ID` (required) and `ALERT_TELEGRAM_BOT_TOKEN`
(optional, falling back to the assistant's own token) from the deployed `.env`,
renders `infra/alertmanager/alertmanager.template.yml`, checks the result with
`amtool`, and installs it as `/etc/demo-firm-agent/alertmanager.yml`, owned by
the uid the container runs as and readable by nothing else. Until that file
exists the deploy starts everything else and says, in one line, that alerting is
off. It never starts Alertmanager against a missing config, because Docker would
create a directory in its place and the container would crash-loop against it.

Grouping, repetition and silences live in that template: one message per alert
name, critical repeated hourly, and no flood of derived alerts while the
application itself is down.

## Logs and correlation

Application logs are single-line JSON. Web requests receive an `X-Request-ID`;
queued Telegram work uses `telegram-<update_id>` so an operator can follow an
update from processing through the agent without logging the staff member's
identity or message body.

Docker stdout is still the fastest place to look at what is happening now:

```bash
docker compose logs --tail=200 legal-bot
```

It is not where history lives. A container's logs die with the container, so a
redeploy erases the evidence of whatever the redeploy was meant to fix. Loki
keeps thirty days of it, the same window Prometheus keeps, so a metric spike and
the log line that explains it never fall out of scope at different times.

Grafana Alloy ships the logs: it discovers containers through the Docker socket,
parses the application's JSON, and labels each line by container, service and
level. Alloy rather than Promtail because Promtail reached end of life in early
2026. Reading that socket is a real privilege, and it is granted deliberately:
the alternative identifies a container by a 64-character id, and a log nobody
can attribute to a service is a log nobody reads.

`level` and `logger` become labels because they have few values. `correlation_id`
and `update_id` never do: one value per conversation would create one stream per
conversation and bring the index down. They stay inside the line, where LogQL
still filters on them:

```logql
{container="demo-firm-v2-legal-bot-1"} |= "telegram-804506605"
{service="legal-bot", level="ERROR"}
```

The no-content policy is unchanged by any of this. Loki stores what the
application already logs, and the application logs no message bodies, no
documents and no prompts.
