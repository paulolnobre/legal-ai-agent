"""Configuration dedicated to the Demo & Associados legal assistant."""
import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"

# Written by CD into the tree it synchronizes, and copied into the image from
# there. It lives beside the package rather than in it: it is deploy metadata,
# not source.
_DEPLOYED_COMMIT_FILE = Path(__file__).parent.parent / ".deployed_sha"


def deployed_commit() -> str:
    """The commit this release was built from, or "unknown".

    A hand-built image and a local run carry no file and say "unknown", which
    is the honest answer — `app_version` is a constant somebody edits, so it
    can agree with a commit it was never built from. Without this, the only way
    to tell what production is running was matching the image's build timestamp
    against a CD run, which stops working the moment two deploys land close
    together or a build is retried.
    """
    try:
        sha = _DEPLOYED_COMMIT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return sha[:12] if sha else "unknown"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "production"] = "development"
    app_version: str = "2.0.0"
    legal_telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    public_base_url: str | None = None
    # Where the pages people open live (/web, /pje), when that is not the
    # webhook's host. Kept apart from PUBLIC_BASE_URL on purpose: that one is
    # where set_webhook points Telegram, and the pages' host does not route
    # the webhook, so moving the pages must never move the bot.
    web_base_url: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    datajud_api_key: str | None = None
    datajud_base_url: str = "https://api-publica.datajud.cnj.jus.br"
    datajud_timeout_seconds: float = 60.0
    datajud_max_processes: int = 3
    datajud_max_movements: int = 12
    # The folder the firm drops documents into, so material can reach the
    # archive without going through Telegram one attachment at a time. It is a
    # prefix of a Magalu Cloud bucket, read with the object storage key: the
    # documents bucket unless INBOX_BUCKET names another, never the backups
    # bucket, since anything here is read and ingested and the two have
    # opposite retention. Off until enabled.
    inbox_enabled: bool = False
    inbox_bucket: str | None = None
    inbox_prefix: str = "entrada/"
    inbox_timeout_seconds: float = 60.0
    # One import reads at most this many new files. A bucket somebody just
    # synchronised a folder into should not become one unbounded run.
    inbox_max_files_per_run: int = 25
    # The scheduled sweep. Off by default: turning it on starts spending money
    # on embeddings without anybody having asked for a run.
    inbox_sweep_enabled: bool = False
    inbox_sweep_minutes: int = 30

    # Which service finds candidate pages. Serper stays the default until a
    # benchmark says otherwise: the roadmap requires comparing legal
    # correctness, currency, authority, citation fidelity, unsupported-claim
    # rate, latency and cost before a swap, not an impression.
    web_search_provider: Literal["serper", "exa"] = "serper"
    exa_api_key: str | None = None
    exa_search_url: str = "https://api.exa.ai/search"
    exa_search_type: str = "auto"
    exa_timeout_seconds: float = 30.0
    exa_max_results: int = 8
    serper_api_key: str | None = None
    serper_search_url: str = "https://google.serper.dev/search"
    serper_timeout_seconds: float = 30.0
    serper_max_results: int = 8
    serper_max_queries: int = 3
    # A search engine stops answering long before it refuses. Measured against
    # the live API on 2026-09-16, one question, one domain, one key: the
    # 304-character query production actually sent returned 0 results, the same
    # query without the parties' names returned 3, and 63 characters of legal
    # terms returned 8. 120 characters is roughly fifteen words, generous for a
    # legal topic and well inside where Google still answers.
    #
    # It also decides what leaves the building. A lawyer states the facts first
    # and asks the legal question last, so a cap this size keeps the search to
    # the doctrine and leaves the client's name behind, without anyone having
    # to build a name detector that would erase courts and statutes too.
    serper_max_query_chars: int = 120
    # The second attempt, when the first found nothing at all. Deliberately
    # near the bare topic, which is where recall was highest in the same test.
    serper_retry_query_chars: int = 60
    serper_allowed_domains: str = (
        "jus.br,gov.br,leg.br,mp.br,def.br,oab.org.br"
    )

    # Reading the official pages the search returned, not just their snippets.
    fetch_enabled: bool = True
    fetch_max_pages: int = 3
    fetch_timeout_seconds: float = 20.0
    fetch_max_bytes: int = 5 * 1024 * 1024
    fetch_max_chars: int = 20_000
    fetch_max_pdf_pages: int = 40
    fetch_max_redirects: int = 3

    # Every individual source below is already capped, but nothing capped their
    # sum: DataJud, DJEN, DJe, web search and up to fetch_max_pages full pages
    # can all be populated for the same question and reach well past 90,000
    # characters together, measured against the current per-source ceilings.
    # This is a character count, like every other limit in this module, not a
    # token count: the project counts tokens only from what the provider
    # reports back after a call, never estimates them beforehand. Roughly
    # 4 characters per token, this sits above the roadmap's pilot-policy
    # external-research ceiling of 18,000 input tokens, wide enough not to
    # trim an ordinary question and tight enough to catch the pathological one.
    external_research_max_context_chars: int = 70_000
    # A browser's string, decided by the firm on 2026-09-21. The agent used to
    # introduce itself by name, and the Planalto resets the connection for
    # that name while answering a browser normally (measured the same day on
    # the Constitution, the Civil Code and the Lei 8.245). The Planalto is the
    # only source of consolidated federal law: the Senado serves a page that
    # needs JavaScript and the Camara only the original publication. So a named
    # agent could not read the one page that says what the law is today. The
    # allowlist is unchanged, and so is how little is read.
    fetch_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )

    # Connecting a lawyer's own Microsoft account, by the device code flow:
    # the agent shows a code, the person signs in at microsoft.com/devicelogin,
    # and the password never reaches this server. ms_tenant is "common" until
    # the firm says whether it has a Microsoft 365 tenant ("organizations" or
    # its tenant id) or personal accounts ("consumers"). The refresh token is
    # encrypted with ms_token_key before it is stored, because the backup
    # leaves this host every night and that key does not.
    ms_enabled: bool = True
    ms_client_id: str | None = None
    ms_tenant: str = "common"
    ms_token_key: str | None = None

    # PJe by MNI, read-only. The password is the most dangerous secret here: a
    # Microsoft token reads somebody's files, this acts as the lawyer in court.
    # Its key is therefore separate, so one leaking does not open the other,
    # and the credential is typed into a page this host serves rather than into
    # the chat, which we neither control nor can wipe. The pilot covers one
    # court, because each has its own endpoint and its own quirks, and one
    # proves the design before it is multiplied.
    pje_enabled: bool = False
    pje_cred_key: str | None = None
    pje_court: str = "TJPA"
    pje_mni_url: str = "https://pje.tjpa.jus.br/pje/intercomunicacao"
    # Ten minutes: long enough to walk to a computer, short enough that a
    # forwarded message is not a standing invitation.
    pje_enrolment_ttl: float = 600.0

    # The web pages: files, the text editor, the editable table. Only people
    # who already have an account with the bot, and only with a second factor:
    # the bot sends a one-time link, which proves control of the Telegram
    # account bound to the staff row, and the page asks for a code from an
    # authenticator app. No password is stored anywhere.
    web_enabled: bool = False
    web_totp_key: str | None = None
    # Eight hours renewed by use, twelve at most. A lawyer opens it in the
    # morning and works the day; asking for a code every hour is how people
    # learn to leave a tab open on a shared machine, which is worse.
    web_session_idle_seconds: float = 8 * 3600.0
    web_session_absolute_seconds: float = 12 * 3600.0
    # How long the link the bot sends stays usable.
    web_login_link_ttl: float = 300.0
    # The document explorer (/web/explorar/): off for everyone, on for the
    # staff ids listed in web_explorer_staff ("1,4"), or on for everyone.
    # Off, its pages answer like pages that do not exist.
    web_explorer: Literal["off", "pilot", "on"] = "off"
    web_explorer_staff: str = ""
    # The assistant in the panel (/web/assistente/), switched like the explorer.
    web_assistant: Literal["off", "pilot", "on"] = "off"
    web_assistant_staff: str = ""
    # Face ID and the app session (/web/ as an installed app), switched like
    # the assistant. A passkey session lasts at most the first number, locks
    # after the second without use, and a passkey may be enrolled only within
    # the third of a full sign-in.
    web_app: Literal["off", "pilot", "on"] = "off"
    web_app_staff: str = ""
    web_app_session_seconds: float = 5 * 3600.0
    web_app_lock_seconds: float = 15 * 60.0
    web_passkey_enrol_seconds: float = 600.0
    # Notices on the locked phone (Web Push), behind web_app and only with all
    # three set. The key pair is drawn on the VPS straight into .env by
    # `python -m core.tools.vapid_keys`, a manual step: it never passes
    # through a session or a commit. The subject is a `mailto:` the push
    # services may write to about these notices.
    web_push_vapid_public_key: str | None = None
    web_push_vapid_private_key: SecretStr | None = None
    web_push_subject: str | None = None
    # A file the panel offers (an export) is sealed for this long, up to this
    # size each and this much live per person; over either, the reply says so.
    panel_file_ttl_hours: int = 24
    panel_file_max_bytes: int = 10 * 1024 * 1024
    panel_files_max_live_bytes: int = 50 * 1024 * 1024
    # A panel request still `sending` after this long was never handed to the
    # queue; one `running` after this long outlived the worker's own limit
    # (300 s) plus a margin. Either is marked failed when the page asks.
    panel_sending_grace_seconds: int = 60
    panel_running_grace_seconds: int = 300 + 120
    # An attachment sent in the panel's chat: at most this size per kind,
    # counted while it arrives; read within the total deadline (longer for
    # an audio file) and never waiting longer than the idle one for a byte;
    # no more than this many arriving at once in the process. Sealed, it
    # waits this many minutes for its message, then lives until its request
    # ends and at most this many hours, and a person holds no more than
    # this much live.
    panel_photo_max_bytes: int = 10 * 1024 * 1024
    panel_voice_max_bytes: int = 5 * 1024 * 1024
    panel_audio_max_bytes: int = 100 * 1024 * 1024
    panel_upload_seconds: float = 120.0
    panel_upload_audio_seconds: float = 600.0
    panel_upload_idle_seconds: float = 30.0
    panel_uploads_in_flight: int = 2
    panel_upload_unattached_minutes: int = 15
    panel_upload_max_hours: int = 24
    panel_uploads_max_live_bytes: int = 150 * 1024 * 1024
    # Reading an attachment in the worker, before the answer's own 300 s:
    # the media stage's deadline per kind, the transcription call's timeout
    # for an audio file, the longest audio (counted on our own re-encoded
    # output), the photo's ceiling in pixels before anything is decoded and
    # its long side once converted, and the one directory where decrypted
    # audio may sit while ffmpeg reads it (emptied at every worker start).
    panel_media_photo_seconds: float = 120.0
    panel_media_voice_seconds: float = 120.0
    panel_media_audio_seconds: float = 900.0
    # A document's extraction, embeddings and stored original: the time a
    # Telegram document has (the whole update's 300 s).
    panel_media_document_seconds: float = 300.0
    panel_transcribe_audio_seconds: float = 600.0
    panel_audio_max_seconds: int = 3600
    panel_photo_max_pixels: int = 40_000_000
    panel_photo_max_side: int = 2048
    panel_media_dir: str = "/tmp/panel-media"

    # Keeping laws in the corpus current. A document linked to its official
    # page with /atualizar is fetched again every law_refresh_days and
    # re-indexed when its text changed. A change larger than
    # law_refresh_max_change, as a share of the stored text, is held for a
    # person instead: a law that grows or shrinks by a fifth overnight is far
    # more often a wrong URL or a truncated page than an amendment.
    law_refresh_enabled: bool = True
    law_refresh_days: int = 7
    law_refresh_max_change: float = 0.2

    # Searching the Diário da Justiça of the court DataJud identified, for that
    # process's publications. The one place a process number reaches a search
    # engine: only a number DataJud confirmed, only in quotes, only on the
    # diary's own domain. See core/integrations/dje.py.
    dje_search_enabled: bool = True

    # The CNJ's national diary (DJEN), read by process number. The same
    # authority as DataJud, so no exception to where a process number may go.
    djen_enabled: bool = True
    djen_base_url: str = "https://comunicaapi.pje.jus.br"
    djen_timeout_seconds: float = 30.0
    # A wider page is fetched than is shown, because the API's order is not
    # documented and the newest publication is the one a deadline runs from.
    djen_fetch_items: int = 30
    djen_max_items: int = 3
    djen_max_chars: int = 6000

    # Telling a lawyer about each new publication, without being asked. Every
    # `publication_watch_minutes` in office hours, the DJEN is asked for what
    # was made available in the last `publication_watch_lookback_days` for each
    # OAB and process somebody follows; a lawyer query reads at most
    # `djen_watch_max_pages` pages of 100.
    # A sentence reaches the reads a command reaches (core/services/
    # legal_assistant/actions.py). Off means today's behaviour exactly; it is
    # turned on only after clients/demo_firm/eval_conversation.py passes.
    conversational_actions: bool = False

    publication_watch_enabled: bool = True
    publication_watch_minutes: int = 120
    publication_watch_lookback_days: int = 3
    djen_watch_max_pages: int = 5

    database_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "demo_firm"
    db_user: str = "demo_firm_agent"
    db_password: str = ""
    db_pool_min: int = 1
    db_pool_max: int = 4
    db_sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"

    llm_simple_provider: Literal["gemini", "openai"] = "openai"
    llm_complex_provider: Literal["gemini", "openai"] = "openai"
    llm_simple_model: str = "gpt-4o-mini"
    llm_complex_model: str = "gpt-4o"
    llm_fallback_model: str = "gpt-4o-mini"
    llm_temperature_simple: float = 0.2
    llm_temperature_complex: float = 0.2
    embed_model: str = "text-embedding-3-small"
    embed_dim: int = 1536

    legal_model: str | None = None
    router_model: str | None = None
    research_model: str | None = None
    drafting_model: str | None = None
    review_model: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = True
    cache_answer_ttl: int = 86_400
    cache_external_answer_ttl: int = 1_800
    cache_embed_ttl: int = 604_800
    queue_active_name: str = "legal:queue:active"
    queue_deferred_name: str = "legal:queue:deferred"
    queue_processing_name: str = "legal:queue:processing"
    queue_dead_letter_name: str = "legal:queue:dead-letter"
    queue_dedupe_ttl: int = 604_800
    queue_block_seconds: int = 5
    queue_max_attempts: int = 3

    office_timezone: str = "America/Sao_Paulo"

    # What the pilot may spend on the model, from the roadmap's pilot policy,
    # counted by the calendar above rather than by the server's clock. Every
    # other limit in this file caps one thing; nothing capped their sum.
    #
    # The monthly token ceilings come to roughly US$13.50 if every token were
    # billed at gpt-4o's rate, which leaves room under the policy's US$25 for
    # accounting differences and for measuring embeddings separately. Serper,
    # DataJud and the other external tools have their own quotas and are not
    # this budget.
    budget_enabled: bool = True
    budget_day_input_tokens: int = 250_000
    budget_day_output_tokens: int = 50_000
    budget_month_input_tokens: int = 3_000_000
    budget_month_output_tokens: int = 600_000
    # What one run reserves before the provider is called. The project has no
    # tokenizer and does not add one, so this is the task class's ceiling from
    # the same policy, not a guess at this prompt: the worst case, returned in
    # full the moment the provider reports what it really used. "complex"
    # carries the largest of the classes that use it (review, 20,000), so it
    # over-reserves an internal research run for the length of that one call.
    budget_worst_input_json: int = 2_000
    budget_worst_input_simple: int = 2_000
    budget_worst_input_complex: int = 20_000
    # What one photo may add to a reading's input: the dearest per-image count
    # among the configured OpenAI models at 2048 px, high detail (gpt-4o-mini
    # counts about 48,000 for eight tiles; gpt-4o about 1,500). Returned the
    # moment the provider reports its real use.
    budget_worst_input_image: int = 50_000
    # Transcription, in seconds of our own re-encoded audio (Whisper bills by
    # duration): about US$0.36 an hour, so 40 hours a month is US$14.40.
    budget_day_audio_seconds: int = 4 * 3600
    budget_month_audio_seconds: int = 40 * 3600
    # The pilot policy's reference prices for gpt-4o, in US$ per million tokens,
    # as of 2026-09-04. The budget counters do not know which model spent what,
    # so /status multiplies everything by the dearer model's rate and reports a
    # ceiling rather than pretending to an exact figure. Revalidate before
    # trusting them: price is external to this repository.
    budget_price_input_per_million: float = 2.50
    budget_price_output_per_million: float = 10.00
    office_start_hour: int = 8
    office_start_minute: int = 0
    office_end_hour: int = 20
    office_end_minute: int = 1

    ocr_enabled: bool = True
    ocr_language: str = "por"
    ocr_pdf_min_chars: int = 80
    max_ocr_pages: int = 100

    web_search_max_uses: int = 5
    web_max_continuations: int = 3
    rag_top_k: int = 8
    chunk_size: int = 1200
    chunk_overlap: int = 200
    max_doc_mb: int = 25
    max_doc_chars: int = 2_000_000
    document_storage_path: str = "/data/documents"

    # Magalu Cloud Object Storage holds the documents the agent works with, in
    # Brazil (br-se1). The endpoint follows the bucket's region
    # (https://<region>.magaluobjects.com) and the region also goes into every
    # request's signature. Everything written there is encrypted here first,
    # because the bucket belongs to a processor the firm does not control.
    #
    # The env vars are MGC_*, not OBJECT_STORE_*: the project also writes to a
    # second provider (Backblaze B2, for backups), and a generic name on one
    # of two providers is exactly the kind of thing a tired 2am edit gets
    # wrong. The Python attribute names stay as they were (object_store_*):
    # this is a storage abstraction internally, and only the Magalu Cloud
    # instance of it today; renaming the attribute too would be a second,
    # unrelated change bundled into a naming fix. B2's own credentials live
    # only in rclone.conf on the VPS, never in .env, so there is no B2_* to
    # name here at all.
    object_store_endpoint: str | None = Field(default=None, validation_alias="MGC_ENDPOINT")
    object_store_bucket: str | None = Field(default=None, validation_alias="MGC_BUCKET")
    object_store_access_key_id: str | None = Field(
        default=None, validation_alias="MGC_ACCESS_KEY_ID")
    object_store_secret_access_key: str | None = Field(
        default=None, validation_alias="MGC_SECRET_ACCESS_KEY")
    object_store_region: str = Field(default="br-se1", validation_alias="MGC_REGION")
    # Without this nothing is written to a bucket at all. There is no default
    # and none is generated: a key that exists without somebody having decided
    # where the spare copy lives is a key that loses the archive.
    doc_master_key: str | None = None

    rate_limit_max: int = 15
    rate_limit_window: int = 60

    # LGPD retention. The access record (who read which document) is kept for
    # the audit period; the question text, which carries the client's own words,
    # is redacted much earlier.
    access_log_retention_days: int = 365
    query_retention_days: int = 90
    retention_hour: int = 3  # local hour of the daily maintenance pass
    max_voice_seconds: int = 300
    max_voice_bytes: int = 20 * 1024 * 1024
    max_question_chars: int = 4000

    metrics_enabled: bool = True
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "legal-ai-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    langsmith_capture_content: bool = False

    @field_validator("office_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid time zone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _guardrails(self):
        if self.app_env == "production":
            if not self.database_url and not self.db_password:
                raise ValueError("DATABASE_URL or DB_PASSWORD is required in production.")
            if not self.legal_telegram_bot_token:
                raise ValueError("LEGAL_TELEGRAM_BOT_TOKEN is required in production.")
            if not self.telegram_webhook_secret:
                raise ValueError("TELEGRAM_WEBHOOK_SECRET is required in production.")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE.")
        start = self.office_start_hour * 60 + self.office_start_minute
        end = self.office_end_hour * 60 + self.office_end_minute
        if not 0 <= start < end <= 24 * 60:
            raise ValueError("Office hours must start before they end.")
        return self

    @model_validator(mode="after")
    def _resolve_agent_models(self):
        if not self.legal_model:
            self.legal_model = self.llm_complex_model
        if not self.router_model:
            self.router_model = self.llm_simple_model
        for role in ("research_model", "drafting_model", "review_model"):
            if not getattr(self, role):
                setattr(self, role, self.legal_model)
        return self

    @model_validator(mode="after")
    def _protect_legal_trace_content(self):
        if not self.langsmith_capture_content and (
            not self.langsmith_hide_inputs or not self.langsmith_hide_outputs
        ):
            raise ValueError(
                "LANGSMITH_CAPTURE_CONTENT=true is required before trace content can be exposed."
            )
        return self


settings = Settings()


def provider_model_problems(config: "Settings") -> list[str]:
    """Configured models that would be sent to a provider they do not belong to.

    Kept out of the Settings validators on purpose: importing the configuration
    must not be fatal, or one wrong line in .env stops the whole test suite from
    being collected and hides the message that explains it. The application
    still refuses to start — see `validate_startup_config`.
    """
    def provider_of(model: str) -> str:
        return "gemini" if model.lower().startswith("gemini") else "openai"

    # Which provider each configured model is actually sent to, mirroring
    # llm._provider_for(): the router and simple chat use the simple provider,
    # everything that reasons over the corpus uses the complex one.
    simple, complex_ = config.llm_simple_provider, config.llm_complex_provider
    pairs = [
        ("LLM_SIMPLE_MODEL", config.llm_simple_model, simple),
        ("LLM_COMPLEX_MODEL", config.llm_complex_model, complex_),
        ("ROUTER_MODEL", config.router_model, simple),
        ("LEGAL_MODEL", config.legal_model, complex_),
        ("RESEARCH_MODEL", config.research_model, complex_),
        ("DRAFTING_MODEL", config.drafting_model, complex_),
        ("REVIEW_MODEL", config.review_model, complex_),
    ]
    problems = [
        f"{name}={model!r} is a {provider_of(model)} model but would be sent to {provider}"
        for name, model, provider in pairs
        if model and provider_of(model) != provider
    ]
    # The fallback only ever covers OpenAI, so a Gemini name there is dead
    # configuration that would surface as an OpenAI 404 under load.
    if config.llm_fallback_model and provider_of(config.llm_fallback_model) != "openai":
        problems.append(
            f"LLM_FALLBACK_MODEL={config.llm_fallback_model!r} is not an OpenAI model, "
            "but the fallback is only used for OpenAI"
        )
    return problems


def missing_provider_credentials(config: "Settings") -> list[str]:
    """A selected provider whose credential is absent.

    Only Gemini is checked, and the asymmetry is deliberate: llm._provider_for
    answers a missing Gemini key by serving OpenAI instead, so the provider
    silently becomes one the firm did not choose. A missing OpenAI key degrades
    the reply but never swaps the provider, and the suite exercises that path on
    purpose.
    """
    selected = {
        "LLM_SIMPLE_PROVIDER": config.llm_simple_provider,
        "LLM_COMPLEX_PROVIDER": config.llm_complex_provider,
    }
    if "gemini" in selected.values() and not config.gemini_api_key:
        chosen = sorted(name for name, value in selected.items() if value == "gemini")
        return [
            f"{' and '.join(chosen)} selects gemini but GEMINI_API_KEY is empty, "
            "so requests would silently be answered by OpenAI"
        ]
    return []


def pages_base_url() -> str | None:
    """The address the one-time links point at: the pages' own host if set,
    otherwise the webhook's, which is what a single-host install uses."""
    return (settings.web_base_url or settings.public_base_url or "").rstrip("/") or None


def push_ready() -> bool:
    """Whether push can be offered: both keys and a `mailto:` subject set.
    Read on every call, so a test or a reload sees the current settings."""
    private = settings.web_push_vapid_private_key
    return bool(settings.web_push_vapid_public_key
                and private is not None and private.get_secret_value()
                and (settings.web_push_subject or "").startswith("mailto:"))


def web_app_on_for(staff_id: int) -> bool:
    """Whether Face ID, the app session and its notices are switched on for
    this person. Decided by id alone, so the worker can ask it before taking
    a connection."""
    if not settings.web_enabled or settings.web_app == "off":
        return False
    if settings.web_app == "on":
        return True
    listed = {part.strip() for part in settings.web_app_staff.split(",")}
    return str(staff_id) in listed


def validate_startup_config(config: "Settings | None" = None) -> None:
    """Refuse to start on a configuration that cannot do what it claims.

    Without this, `llm.py` quietly answers with a different model than the one
    configured, and the firm believes it is running what .env says.
    """
    config = config or settings
    problems = provider_model_problems(config) + missing_provider_credentials(config)
    if problems:
        raise RuntimeError(
            "Incompatible provider/model configuration in .env:\n  - "
            + "\n  - ".join(problems)
        )



def _configure_langsmith_environment() -> None:
    """Expose validated settings to the SDK, including local `.env` values."""
    values = {
        "LANGSMITH_TRACING": str(settings.langsmith_tracing).lower(),
        "LANGSMITH_PROJECT": settings.langsmith_project,
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
        "LANGSMITH_HIDE_INPUTS": str(settings.langsmith_hide_inputs).lower(),
        "LANGSMITH_HIDE_OUTPUTS": str(settings.langsmith_hide_outputs).lower(),
    }
    if settings.langsmith_api_key:
        values["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    for name, value in values.items():
        os.environ.setdefault(name, value)


_configure_langsmith_environment()

LEGAL_TELEGRAM_TOKEN = settings.legal_telegram_bot_token
LEGAL_TELEGRAM_URL = f"https://api.telegram.org/bot{LEGAL_TELEGRAM_TOKEN}"
OPENAI_KEY = settings.openai_api_key
GEMINI_KEY = settings.gemini_api_key
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

EMBED_MODEL = settings.embed_model
EMBED_DIM = settings.embed_dim
RAG_TOP_K = settings.rag_top_k
CHUNK_SIZE = settings.chunk_size
CHUNK_OVERLAP = settings.chunk_overlap
