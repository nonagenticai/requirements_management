"""Application settings using Pydantic BaseSettings.

All environment-specific values (service URLs, observability endpoints) are
loaded from environment variables. Deployments supply their own values in the
container environment. Source carries empty-string placeholders only — no
cluster-internal DNS literals — and a production-fail-fast validator
(``model_post_init``) refuses to start if any required field is missing.

Pattern: see ``inference-router/src/settings.py`` and
``gitops-production/docs/config-externalization-policy.md``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["observe", "enforce"]
_AUTH_MODES: tuple[str, ...] = ("observe", "enforce")


def parse_auth_mode(name: str, raw: object) -> AuthMode:
    """Strictly parse an observe/enforce auth switch (fail CLOSED on a typo).

    Unset/empty -> ``observe``; otherwise strip + lowercase and accept only
    ``observe`` or ``enforce``. Anything else raises ``ValueError``: the old
    reader treated every non-``enforce`` value as observe, so ``enforced`` or
    ``true`` silently left the gate open. Shared by the Settings validator
    (crashes boot) and the per-call reader in ``auth/governed_scope.py``.
    """
    value = ("" if raw is None else str(raw)).strip().lower()
    if value in ("", "observe"):
        return "observe"
    if value == "enforce":
        return "enforce"
    raise ValueError(f"{name}={raw!r} is not one of {list(_AUTH_MODES)} (or unset)")


class Settings(BaseSettings):
    """Configuration for the requirements-management service.

    Settings can be overridden via environment variables. Plain (un-prefixed)
    names are accepted because most existing call sites already read them with
    ``os.getenv("OTLP_TRACES_ENDPOINT", ...)``-style lookups; they remain the
    canonical names in the gitops Deployment manifest.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ─────────────────────────────────────────────────────────────
    # Postgres DSN. Placeholder only; real value comes from the Deployment env
    # block in gitops-production. Production startup fails (model_post_init) if
    # missing so the service never silently targets the localhost fallback DSN
    # baked into db.py.
    database_url: str = Field(
        default="",
        description="Postgres DSN (postgresql:// auto-rewritten to postgresql+psycopg://)",
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )

    # ── System-tenant inheritance ────────────────────────────────────────────
    # The SYSTEM+IMPROVEMENT tenant holds system assets. Every other tenant
    # READS its own rows plus this tenant's PUBLISHED, tenant-visible rows
    # (``repositories.visibility.scope(include_system=True)``); writes are
    # always own-tenant only. Mirrors ``procedure_management/src/config.py::system_tenant_id``.
    #
    # ⚠️ The Postgres RLS read policy ``system_published_read`` (migration
    # ``2026_09_27_0044``) FREEZES this uuid into its predicate. Changing this
    # env var without a matching migration widens the app-level scope to a
    # tenant RLS will still hide — every inherited row reads as absent.
    system_tenant_id: str = Field(
        default="0195f48b-d000-7000-8000-000000000001",
        description=(
            "SYSTEM+IMPROVEMENT tenant uuid whose PUBLISHED rows every tenant inherits (read-only)"
        ),
        validation_alias=AliasChoices("SYSTEM_TENANT_ID", "system_tenant_id"),
    )

    # ── Who may inherit system rows ──────────────────────────────────────────
    # Owner rule 2026-09-30: system + improvement data live on their own
    # tenants and must NEVER be visible to a customer tenant. Only these
    # PLATFORM tenants widen to the system tenant's PUBLISHED rows; every other
    # tenant reads its own rows only (``repositories.visibility.scope``).
    #
    # Env: ``PLATFORM_TENANT_IDS`` — comma-separated uuids. The default is the
    # full platform set (NOT empty) so deploying this code before the env var
    # exists changes nothing for platform tenants.
    #
    # ⚠️ The Postgres RLS policy ``system_published_read`` (migration
    # ``2026_09_27_0044``) still admits EVERY tenant to published system rows
    # at DB level; this setting is the app-level fence only.
    platform_tenant_ids: str = Field(
        default=(
            "0195f48b-d000-7000-8000-000000000001,"  # system
            "0195f48b-d000-7000-8000-000000000002,"  # workflow-improvement
            "0195f48b-d000-7000-8000-000000000003,"  # execution
            "0195f48b-d000-7000-8000-000000000004,"  # prompt-improvement
            "0195f48b-d000-7000-8000-000000000005,"  # procedure-improvement
            "019de842-c92a-7e37-8fdd-eb0d8c5396f9"  # n8n-dev (AA conveyor)
        ),
        description=(
            "Comma-separated PLATFORM tenant uuids allowed to inherit the system "
            "tenant's PUBLISHED rows; every other tenant sees own rows only"
        ),
        validation_alias=AliasChoices("PLATFORM_TENANT_IDS", "platform_tenant_ids"),
    )

    @property
    def platform_tenant_id_set(self) -> frozenset[str]:
        """Parsed, canonicalised (lower-case uuid) platform tenant set."""
        return frozenset(
            str(UUID(p.strip())) for p in self.platform_tenant_ids.split(",") if p.strip()
        )

    # ── Upstream service URLs ────────────────────────────────────────────────
    # Placeholders only; real values come from the Deployment env block in
    # gitops-production. Production startup fails (model_post_init) if missing.

    license_introspect_url: str = Field(
        default="",
        description="License-server introspection endpoint",
        validation_alias=AliasChoices("LICENSE_INTROSPECT_URL", "license_introspect_url"),
    )

    # Optional: enables architecture conformance (as-built code-graph facts).
    # Not in _REQUIRED_IN_PRODUCTION — the service runs fully without it; only
    # the /architecture/tools/conformance endpoint needs it (runtime-guarded).
    coding_analysis_url: str = Field(
        default="",
        description="Base URL for coding-analysis (code-graph as-built facts)",
        validation_alias=AliasChoices("CODING_ANALYSIS_URL", "coding_analysis_url"),
    )

    # This service's OWN licence token, presented to coding-analysis
    # as `Authorization: Bearer <token>` so those services can verify WHO is
    # calling. It is NOT a tenant: the customer tenant still rides the bare
    # X-Tenant-Id header, and coding trusts that asserted tenant only because
    # this token's subject is on their CODING_ONBEHALF_SUBJECTS allowlist.
    #
    # Optional and defaulting to empty on purpose. The coding client sends the
    # Authorization header only when it is set, so an unset token degrades to
    # exactly the pre-2026-08-15 behaviour (header-only, which now 401s) rather
    # than crashing at import. Not in _REQUIRED_IN_PRODUCTION: the service runs
    # fully without it, and only the coding-backed conformance endpoint needs it.
    coding_service_token: str = Field(
        default="",
        description="This service's own licence token for coding-* calls (svc-requirements)",
        validation_alias=AliasChoices("CODING_SERVICE_TOKEN", "coding_service_token"),
    )

    # This service's catalogue app id, sent as `X-App-Id` on outbound coding-*
    # calls (src/clients/_coding_auth.py). `X-App-Id` names WHICH application
    # asked; it is attribution data for billing, never a credential, and it is
    # independent of the licence.
    #
    # The value is the Requirements Module app id — the SAME id the removed LLM
    # lane sent on every worker_management call (`requirements_module_app_id`,
    # removed with that lane in #238). Restored rather than invented: it is a
    # registered catalogue app (gitops SESSION-PLAN-2026-09-06.md:25 bills
    # 06954722-ce8f-7ff3-80c2-23e49a36e26f as Requirements Module), and it is
    # spelled out in this repo at
    # docs/superpowers/specs/2026-10-26-rm-llm-actions-reference.md:87.
    #
    # ⛔ Do NOT default this inside the header builder, and do NOT substitute a
    # service name ("requirements-api") here: a self-named app id collapses every
    # distinct upstream origin into one bucket while reading as 100% coverage.
    # Empty is honest — the header is then omitted entirely.
    requirements_module_app_id: str = Field(
        default="06954722-ce8f-7ff3-80c2-23e49a36e26f",
        description=(
            "This service's catalogue app id, sent as X-App-Id on coding-* calls; "
            "defaults to the Requirements Module app id"
        ),
        validation_alias=AliasChoices("REQUIREMENTS_MODULE_APP_ID", "requirements_module_app_id"),
    )

    # ── Observability ────────────────────────────────────────────────────────

    # Event-loop lag probe. See `event_loop_lag_loop` in lifespan.py and issue #111.
    #
    # This service runs ONE replica with a `timeoutSeconds: 5, failureThreshold: 3`
    # readiness probe, so ~30s of event-loop blocking silently removes it from
    # Service endpoints. The request middleware's `duration_s` cannot tell
    # "this handler was slow" from "the loop was busy elsewhere", which is why
    # #111 has two refuted hypotheses and no root cause. This measures blocking
    # directly.
    event_loop_lag_interval_s: float = Field(
        default=0.5,
        description="Seconds between event-loop lag samples; <=0 disables the probe",
        validation_alias=AliasChoices("EVENT_LOOP_LAG_INTERVAL_S", "event_loop_lag_interval_s"),
    )
    # 1.0s is deliberately well below the 5s probe timeout: the point is to catch
    # the loop stalling BEFORE it is long enough to fail a readiness probe, so the
    # warning is a leading indicator rather than a post-mortem of an outage.
    event_loop_lag_warn_s: float = Field(
        default=1.0,
        description="Log a WARNING when measured event-loop lag reaches this many seconds",
        validation_alias=AliasChoices("EVENT_LOOP_LAG_WARN_S", "event_loop_lag_warn_s"),
    )

    # ── Metrics listener ─────────────────────────────────────────────────────
    # ⛔ Deliberately NOT the API port (8921). Scrape admission is PORT-scoped,
    # so /metrics on 8921 would require opening the whole API to the pod CIDR.
    # See src/metrics_server.py. Changing this is a multi-place change: this
    # default, the Deployment containerPort/Service port, and the port-level
    # PeerAuthentication + allow-prometheus-scrape NetworkPolicy in gitops.
    metrics_port: int = Field(
        default=9921,
        gt=0,
        description=(
            "Port for the DEDICATED plaintext Prometheus listener. Must never be "
            "the API port: the scrape relax is port-scoped."
        ),
        validation_alias=AliasChoices("METRICS_PORT", "metrics_port"),
    )

    # ── Modeling (target spec §7.5) ──────────────────────────────────────────
    # Synchronous validation, exchange export/import and rendering refuse a
    # model with more concepts plus relationships than this; they never truncate.
    model_sync_max_concepts: int = Field(
        default=5000,
        gt=0,
        description=(
            "Largest model (concepts + relationships) a SYNCHRONOUS validation "
            "accepts. Larger inputs are refused with 422 input_too_large — never "
            "truncated or sampled (target spec §7.5)."
        ),
        validation_alias=AliasChoices("MODEL_SYNC_MAX_CONCEPTS", "model_sync_max_concepts"),
    )
    model_async_max_concepts: int = Field(
        default=100_000,
        gt=0,
        description=(
            "Largest model (concepts + relationships) that can be SEALED, and largest "
            "sealed revision a BACKGROUND validation accepts (202, run queued). Above "
            "MODEL_SYNC_MAX_CONCEPTS a revision run is queued; above this both are "
            "refused with 422 input_too_large."
        ),
        validation_alias=AliasChoices("MODEL_ASYNC_MAX_CONCEPTS", "model_async_max_concepts"),
    )
    model_async_max_queued: int = Field(
        default=4,
        gt=0,
        description=(
            "Most background validation runs this process holds at once (queued + "
            "running; each holds its revision manifest in memory). Beyond it a run "
            "request is refused with 503 validation_queue_full and Retry-After."
        ),
        validation_alias=AliasChoices("MODEL_ASYNC_MAX_QUEUED", "model_async_max_queued"),
    )
    model_seal_statement_timeout_ms: int = Field(
        default=60_000,
        gt=0,
        description=(
            "statement_timeout (ms, SET LOCAL: the seal transaction only) of sealing a "
            "revision, which holds the model row FOR UPDATE and a request-pool connection "
            "while it builds the manifest. A timeout is 503 seal_timeout."
        ),
        validation_alias=AliasChoices(
            "MODEL_SEAL_STATEMENT_TIMEOUT_MS", "model_seal_statement_timeout_ms"
        ),
    )
    exchange_import_max_bytes: int = Field(
        default=25 * 1024 * 1024,
        gt=0,
        description=(
            "Largest Open Group exchange document an import accepts (bytes). Larger "
            "bodies are refused with 413 exchange_too_large before parsing."
        ),
        validation_alias=AliasChoices("EXCHANGE_IMPORT_MAX_BYTES", "exchange_import_max_bytes"),
    )

    otlp_traces_endpoint: str = Field(
        default="",
        description="OTLP HTTP traces endpoint (e.g. Grafana Alloy)",
        validation_alias=AliasChoices("OTLP_TRACES_ENDPOINT", "otlp_traces_endpoint"),
    )

    # ── Runtime context ──────────────────────────────────────────────────────

    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("ENVIRONMENT", "environment"),
    )

    # G3 / R313 §1(b): a non-fr (conveyor) write to requirements / principles / decisions
    # without a project scope. ``observe`` logs + counts and allows; ``enforce`` refuses
    # 422 ``conveyor_write_requires_project_scope``. Read per call in auth/governed_scope.py.
    governed_write_scope_mode: AuthMode = Field(
        default="observe",
        validation_alias=AliasChoices("GOVERNED_WRITE_SCOPE_MODE", "governed_write_scope_mode"),
    )

    @field_validator("governed_write_scope_mode", mode="before")
    @classmethod
    def _governed_scope_mode_must_be_known(cls, v: object) -> AuthMode:
        return parse_auth_mode("GOVERNED_WRITE_SCOPE_MODE", v)

    @field_validator("system_tenant_id")
    @classmethod
    def _system_tenant_must_be_a_uuid(cls, v: str) -> str:
        """Crash at startup on a non-uuid: ``tenant_id`` is a uuid column, so a
        name here would silently inherit nothing (it can never equal a row)."""
        return str(UUID(v.strip()))

    @field_validator("platform_tenant_ids")
    @classmethod
    def _platform_tenants_must_be_uuids(cls, v: str) -> str:
        """Crash at startup on a non-uuid entry (a name could never match a row)
        or an explicitly EMPTY value: an empty set would silently cut every
        platform tenant (incl. the n8n conveyor) off from system rows. Unset
        keeps the default; to change the set, list the uuids."""
        ids = [str(UUID(p.strip())) for p in v.split(",") if p.strip()]
        if not ids:
            raise ValueError(
                "PLATFORM_TENANT_IDS is set but empty; unset it for the default "
                "platform set or list at least one tenant uuid"
            )
        return ",".join(ids)

    _REQUIRED_IN_PRODUCTION: ClassVar[tuple[str, ...]] = (
        "database_url",
        "license_introspect_url",
        "otlp_traces_endpoint",
    )

    def model_post_init(self, __context: object) -> None:
        if self.environment != "production":
            return
        missing = [n for n in self._REQUIRED_IN_PRODUCTION if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                f"requirements-management settings missing required fields in production: {missing}. "
                "Set them via env vars on the Deployment (see gitops-production)."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
