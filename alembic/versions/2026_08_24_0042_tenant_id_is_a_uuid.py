"""tenant_id becomes a real ``uuid`` column — the UUIDv7 plan's Phase 4.

``2026_04_27_0019_add_tenant_name_column`` added ``tenant_name`` and said, in
its own docstring, that ``tenant_id`` "remains VARCHAR(255) for now — UUID type
flip is Phase 4". This is Phase 4. ``tenant_name`` exists precisely so
``tenant_id`` can stop carrying human-readable labels, and every affected row
already has its original label preserved there, so nothing is lost here.

WHY THIS IS THE FIX AND NOT A SHAPE PREFERENCE. The canonical tenant registry
is ``licenses.tenants(id uuid, name varchar, keycloak_realm varchar)`` — the
identifier is the UUID; ``name`` is an alias licences resolves. Measured
against production on 2026-08-24, this database had ONE tenant living under
TWO identifiers at the same time:

    n8n-dev                               ->  60 rows
    019de842-c92a-7e37-8fdd-eb0d8c5396f9  -> 157 rows   (the same tenant)

in the same tables. Nothing joins those two populations. That split is the
defect this migration closes; the type change is what stops it recurring.

WHAT ABOUT NAMES AT THE BOUNDARY. They still work. PR #129 was right that the
discriminator is *resolvability against licences*, not shape — so a caller may
still send ``X-Tenant-Id: n8n-dev`` and ``auth/tenant_resolution.py`` resolves
it to the UUID before anything touches the database. What changes is only what
gets STORED. The two are no longer the same question.

THE 12 NON-UUID VALUES FOUND IN PRODUCTION (147 rows across 23 tables):
``n8n-dev`` resolves in the registry. The other eleven —
``1764108879001552894``, ``1768129775626355922``, ``1769980520185202330``,
``1770309193396389488``, ``1770309213185115866``, ``1777679506996200476``,
``1797416352504349851``, ``__system_workflow__``, ``asset-improve-live``,
``auto``, ``smoke-plan-7`` — resolve to nothing at licences (verified live).
They are not deleted and they are not merged: each gets a deterministic UUIDv5
of its own label, so the mapping is 1:1 and every isolation boundary that
existed before this migration exists after it, unchanged. ``tenant_id_crosswalk``
records every remap, so any of them can be re-pointed at a real registry UUID
later with a one-row update and a re-run of the same UPDATE …FROM.

Derived ids are v5, not v7, on purpose: a v7 encodes a mint time, and there is
no honest mint time for a value that was already in the table. A genuine v7 can
only come from registering the tenant in licences — an owner decision, not a
migration's.

ORDER MATTERS, AND IT IS NOT NEGOTIABLE. Measured on Postgres 18:
``ALTER TABLE … ALTER COLUMN tenant_id TYPE uuid`` fails outright while a
policy references the column ("cannot alter type of a column used in a policy
definition"). So: drop policies -> remap data -> alter type -> recreate
policies. The recreated DDL is INLINED below (2026-11-01, frozen from
``kag_common.database.rls`` 0.37.0): it emits
``… = NULLIF(current_setting('app.current_tenant', true), '')::uuid`` once the
column is no longer a ``String``. It is inlined rather than imported because a
historical migration must not depend on an external package — that package's
output changed across releases once already and silently altered what an earlier
revision did. That also repairs ``principle_conflicts``, whose policy was
hand-written as a text comparison in ``2026_07_09_0031:33-35`` and would
otherwise have been left comparing ``uuid = text``.

RLS is only re-applied to the tables that actually had it before this
migration ran — read from ``pg_class.relrowsecurity``, not from a hardcoded
list. ``conflict_reconcile_watermark`` and ``conflict_reconcile_failure`` carry
``tenant_id`` but are deliberately outside the backstop (they are operational
cursors swept cross-tenant by an owner connection); reading the live state
keeps them that way instead of silently enrolling them.

Revision ID: 2026_08_24_0042
Revises: 2026_08_24_0041
Create Date: 2026-08-24
"""

import importlib
import pkgutil
import uuid

import sqlalchemy as sa

import models
from alembic import op
from db import Base

for _m in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f"models.{_m.name}")

revision = "2026_08_24_0042"
down_revision = "2026_08_24_0041"
branch_labels = None
depends_on = None

CROSSWALK = "tenant_id_crosswalk"

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# The one non-UUID value in production that the licences registry resolves.
# Verified live 2026-08-24: GET /tenants/n8n-dev/byo-mode -> 200
# {"tenant_id":"019de842-c92a-7e37-8fdd-eb0d8c5396f9","name":"n8n-dev"}.
# Hardcoded rather than looked up: a migration must not depend on a network
# call, and this pairing is a fact about data that already exists.
REGISTRY: dict[str, str] = {"n8n-dev": "019de842-c92a-7e37-8fdd-eb0d8c5396f9"}

# Shared with ``tests/tenants.py`` so a label maps to the same UUID in a test
# as it does in this repair. Never change it.
TENANT_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def canonical_tenant_uuid(legacy: str) -> tuple[str, str]:
    """``(uuid, source)`` for a legacy tenant value. Pure — unit-testable."""
    if legacy in REGISTRY:
        return REGISTRY[legacy], "registry"
    return str(uuid.uuid5(TENANT_NAMESPACE, legacy)), "derived"


def _alterable(conn: sa.Connection, tables: list[str]) -> list[str]:
    """Of ``tables``, the ones this role may actually ``ALTER``.

    ⛔ A MIGRATION MUST NOT DIE ON A TABLE THE SERVICE DOES NOT OWN.

    Measured in production 2026-08-25: this migration found
    ``outage_misclassification_repair_20260824`` — an ad-hoc repair snapshot a
    previous session had created through ``sudo -u postgres psql``, so owned by
    ``postgres`` while every service table is owned by ``requirements_user``.
    The ALTER raised ``must be owner of table``, the init container
    CrashLoopBackOff'd, and because the deployment strategy is ``Recreate`` the
    old pod was already gone — a 17-minute outage caused by a table holding no
    service data at all.

    ``pg_has_role(…, 'USAGE')`` is the exact privilege ``ALTER TABLE`` requires:
    be the owner, or be a member of the owning role. Anything else is skipped
    and named in the log rather than taking the deployment down with it. Skipping
    is safe: such a table is by definition not one this service manages, and the
    crosswalk still records every legacy value, so it can be converted by hand
    later with the same mapping.
    """
    if not tables:
        return []
    rows = conn.execute(
        sa.text(
            "SELECT c.relname, pg_has_role(current_user, c.relowner, 'USAGE') "
            "FROM pg_class c WHERE c.relnamespace = current_schema()::regnamespace "
            "AND c.relname = ANY(:names) ORDER BY 1"
        ),
        {"names": tables},
    )
    owned, skipped = [], []
    for name, may_alter in rows:
        (owned if may_alter else skipped).append(name)
    if skipped:
        # print(), not logger.warning(): measured 2026-08-25, a warning on ANY
        # logger is swallowed inside the migration process — this repo's
        # alembic.ini carries no logging config, env.py's fileConfig bails, and
        # by the time a migration runs something in the import chain has already
        # configured logging such that nothing reaches stderr. The same call
        # emits fine from a bare interpreter, which is exactly how you convince
        # yourself a silent skip is loud. An init container's stdout is the one
        # channel an operator will actually read.
        print(
            f"⚠️  tenant_id flip SKIPPING {len(skipped)} table(s) this role cannot "
            f"ALTER: {', '.join(skipped)}. They keep a VARCHAR tenant_id. Convert "
            f"by hand (or transfer ownership) if they matter; {CROSSWALK} holds "
            "the value mapping.",
            flush=True,
        )
    return owned


def _tenant_tables(conn: sa.Connection) -> list[str]:
    """Live tables carrying a non-uuid ``tenant_id`` that this role may ALTER.

    Read from ``information_schema`` rather than ``Base.metadata`` because the
    metadata has *already* been flipped to ``Uuid`` by the model change that
    ships with this migration — it can no longer tell us what the database
    currently holds. It also catches tables created outside the ORM, which is
    exactly why the ownership filter below is not optional.
    """
    rows = conn.execute(
        sa.text(
            "SELECT c.table_name FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = current_schema() AND c.column_name = 'tenant_id' "
            "  AND t.table_type = 'BASE TABLE' AND c.data_type <> 'uuid' "
            "  AND c.table_name <> :crosswalk "
            "ORDER BY 1"
        ),
        {"crosswalk": CROSSWALK},
    )
    return _alterable(conn, [r[0] for r in rows])


def _rls_enabled(conn: sa.Connection, tables: list[str]) -> set[str]:
    if not tables:
        return set()
    rows = conn.execute(
        sa.text(
            "SELECT relname FROM pg_class WHERE relkind = 'r' "
            "AND relnamespace = current_schema()::regnamespace "
            "AND relrowsecurity AND relname = ANY(:names)"
        ),
        {"names": tables},
    )
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        CROSSWALK,
        sa.Column("legacy_value", sa.String(length=255), primary_key=True),
        # Deliberately NOT named ``tenant_id``: the RLS helper (inlined below,
        # frozen from ``kag_common.database.rls``) enrols every table with a
        # ``tenant_id`` column, and this crosswalk is an operational lookup that
        # must stay readable cross-tenant.
        sa.Column("tenant_uuid", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    _global_toc_settings_row_becomes_null(conn)

    tables = _tenant_tables(conn)
    if not tables:  # already uuid everywhere (a re-run, or a fresh DB)
        return

    had_rls = _rls_enabled(conn, tables)

    # 1. Every distinct non-UUID value still in the data.
    union = " UNION ".join(
        f"SELECT DISTINCT tenant_id FROM {t} WHERE tenant_id !~ :re" for t in tables
    )
    legacy = [r[0] for r in conn.execute(sa.text(union), {"re": _UUID_RE})]

    # 2. Record the mapping before touching a single row, so the repair is
    #    auditable and reversible even if a later step fails.
    for value in sorted(legacy):
        mapped, source = canonical_tenant_uuid(value)
        conn.execute(
            sa.text(
                f"INSERT INTO {CROSSWALK} (legacy_value, tenant_uuid, source, note) "  # noqa: S608
                "VALUES (:v, CAST(:u AS uuid), :s, :n) ON CONFLICT (legacy_value) DO NOTHING"
            ),
            {
                "v": value,
                "u": mapped,
                "s": source,
                "n": (
                    "resolved against the licences tenant registry"
                    if source == "registry"
                    else "no licences tenant resolves this value; deterministic UUIDv5 of the "
                    "label, 1:1 so the isolation boundary is unchanged"
                ),
            },
        )

    # 3. Fold the operational cursors first. Consolidating ``n8n-dev`` into its
    #    registry UUID is a real MERGE of two rows that were always one tenant,
    #    and ``conflict_reconcile_watermark`` has ``tenant_id`` as its whole
    #    primary key. Measured on a clone of production: this is the ONLY table
    #    where the merge collides — every domain table remaps cleanly.
    _merge_operational_cursors(conn)

    # 4. Remap. tenant_name already holds the original label on every row.
    #    Each table gets a SAVEPOINT so a unique-constraint collision produces an
    #    actionable message naming the table, instead of a bare psycopg
    #    traceback from somewhere inside alembic. It is deliberately NOT
    #    swallowed: a collision means two populations that this migration
    #    believes are one tenant hold the same slug, and quietly dropping or
    #    re-splitting one of them would lose governance data. Production has
    #    none (measured 2026-08-24); an operator must look at any that appear.
    for table in tables:
        sp = conn.begin_nested()
        try:
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET tenant_id = x.tenant_uuid::text "  # noqa: S608
                    f"FROM {CROSSWALK} x WHERE {table}.tenant_id = x.legacy_value"
                )
            )
            sp.commit()
        except sa.exc.IntegrityError as exc:  # pragma: no cover - guarded, not expected
            sp.rollback()
            raise RuntimeError(
                f"tenant_id remap collided on {table!r}: two tenant identifiers that "
                f"{CROSSWALK} maps to the same UUID already hold the same unique key. "
                f"Inspect {CROSSWALK} and {table}, decide which row survives, then re-run. "
                f"Underlying error: {exc.orig}"
            ) from exc

    # 4. Policies block ALTER TYPE — drop, alter, recreate.
    # Drop what the DATABASE actually has, not what the models think it has.
    # Reading ``had_rls`` from ``pg_class`` while dropping from ``Base.metadata``
    # is asymmetric, and a tenant table that exists outside the ORM (prod
    # already has one: ``outage_misclassification_repair_20260824``) would keep
    # its policy and abort the ALTER with "cannot alter type of a column used in
    # a policy definition".
    for table, policy in _live_policies(conn, tables):
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON {table}')
    for table in sorted(had_rls):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for table in tables:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN tenant_id TYPE uuid USING tenant_id::uuid")

    # NULLS NOT DISTINCT, or Postgres treats every NULL as unique and the table
    # accepts unlimited "global" rows — see TocSettings' docstring.
    op.drop_constraint("uq_toc_settings_tenant_project", "toc_settings", type_="unique")
    op.execute(
        "ALTER TABLE toc_settings ADD CONSTRAINT uq_toc_settings_tenant_project "
        "UNIQUE NULLS NOT DISTINCT (tenant_id, project_id)"
    )

    for stmt in _upgrade_statements(
        Base.metadata, exclude={t for t in _tenant_table_names(Base.metadata) if t not in had_rls}
    ):
        op.execute(stmt)

    # Tables that had RLS but are not in the ORM get their strict policy back by
    # hand — the helper above only knows about metadata tables.
    guc = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
    for table in sorted(had_rls - set(_tenant_table_names(Base.metadata))):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {guc}) WITH CHECK (tenant_id = {guc})"
        )


def _live_policies(conn: sa.Connection, tables: list[str]) -> list[tuple[str, str]]:
    """``(table, policyname)`` for every policy the database currently holds."""
    if not tables:
        return []
    rows = conn.execute(
        sa.text(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname = current_schema() AND tablename = ANY(:names) "
            "ORDER BY 1, 2"
        ),
        {"names": tables},
    )
    return [(r[0], r[1]) for r in rows]


def _global_toc_settings_row_becomes_null(conn: sa.Connection) -> None:
    """``toc_settings``' global-defaults row moves from ``'__global__'`` to NULL.

    ``models.toc.GLOBAL_SETTINGS_TENANT`` was the literal string ``'__global__'``
    — not expressible in a ``uuid`` column. Replacing it with a sentinel UUID
    would have preserved a bug: the RLS helper (inlined below) gives a NOT NULL
    ``tenant_id`` a STRICT policy, so under ``TENANT_RLS_MODE=enforce`` the
    global row was invisible to every tenant and the global tier silently
    resolved to nothing. A NULLABLE ``tenant_id`` gets the "global-or-own"
    policy instead (``tenant_id IS NULL OR tenant_id = <guc>``
    for reads, scoped for writes), which is what this row always needed.

    Runs before the crosswalk is built so ``'__global__'`` is never mistaken for
    a tenant and handed a derived UUID. ``toc_settings`` is empty in production
    (measured 2026-08-24), so this is schema-only there; other environments may
    hold the row.
    """
    op.alter_column("toc_settings", "tenant_id", nullable=True)
    op.execute("UPDATE toc_settings SET tenant_id = NULL WHERE tenant_id = '__global__'")


def _merge_operational_cursors(conn: sa.Connection) -> None:
    """Fold a legacy tenant's cursor rows into the survivor's, then drop them.

    Both tables here are regenerable operational cursors, not tenant data, so a
    merge is safe — but only if it errs toward doing MORE work, never less.

    ``conflict_reconcile_watermark`` holds ``last_seen_updated_at``: the
    reconciler evaluates only procedures newer than it. Keeping the HIGHER of
    two watermarks would silently skip every procedure that changed in between,
    so the merge keeps the LOWER — and NULL ("never run") beats any timestamp,
    because NULL makes the next cycle evaluate the tenant's whole procedure
    list. The cost is one re-evaluation; the alternative is an unexamined
    conflict.

    ``conflict_reconcile_failure`` holds two failure streaks that drive
    quarantine. Keeping the smaller would hand a broken procedure a fresh start,
    so the merge keeps the MAX of each streak, the earliest ``first_failed_at``
    and the latest ``last_failed_at``/``last_error``.
    """
    conn.execute(
        sa.text(f"""
        UPDATE conflict_reconcile_watermark w SET last_seen_updated_at = m.merged
        FROM (
            SELECT x.tenant_uuid::text AS survivor,
                   CASE WHEN bool_or(w2.last_seen_updated_at IS NULL) THEN NULL
                        ELSE min(w2.last_seen_updated_at) END AS merged
            FROM conflict_reconcile_watermark w2
            JOIN {CROSSWALK} x
              ON w2.tenant_id IN (x.legacy_value, x.tenant_uuid::text)
            GROUP BY 1
        ) m
        WHERE w.tenant_id = m.survivor
        """)  # noqa: S608
    )
    conn.execute(
        sa.text(f"""
        UPDATE conflict_reconcile_failure f SET
            consecutive_failures = m.cf,
            consecutive_transient_failures = m.ctf,
            first_failed_at = m.ffa,
            last_failed_at = m.lfa,
            last_error = m.le,
            last_failure_kind = m.lk
        FROM (
            SELECT x.tenant_uuid::text AS survivor, f2.counterparty_id,
                   max(f2.consecutive_failures) AS cf,
                   max(f2.consecutive_transient_failures) AS ctf,
                   min(f2.first_failed_at) AS ffa,
                   max(f2.last_failed_at) AS lfa,
                   (array_agg(f2.last_error ORDER BY f2.last_failed_at DESC NULLS LAST))[1] AS le,
                   (array_agg(f2.last_failure_kind ORDER BY f2.last_failed_at DESC NULLS LAST))[1]
                       AS lk
            FROM conflict_reconcile_failure f2
            JOIN {CROSSWALK} x
              ON f2.tenant_id IN (x.legacy_value, x.tenant_uuid::text)
            GROUP BY 1, 2
        ) m
        WHERE f.tenant_id = m.survivor AND f.counterparty_id = m.counterparty_id
        """)  # noqa: S608
    )
    # The legacy-side rows have now been folded in; delete any that would
    # collide with an existing survivor. Written out rather than generated: the
    # two keys differ, and a generated DELETE here is exactly the kind of clever
    # string-building that quietly deletes the wrong side.
    conn.execute(
        sa.text(f"""
        DELETE FROM conflict_reconcile_watermark t USING {CROSSWALK} x
        WHERE t.tenant_id = x.legacy_value
          AND EXISTS (
              SELECT 1 FROM conflict_reconcile_watermark s
              WHERE s.tenant_id = x.tenant_uuid::text
          )
        """)  # noqa: S608
    )
    conn.execute(
        sa.text(f"""
        DELETE FROM conflict_reconcile_failure t USING {CROSSWALK} x
        WHERE t.tenant_id = x.legacy_value
          AND EXISTS (
              SELECT 1 FROM conflict_reconcile_failure s
              WHERE s.tenant_id = x.tenant_uuid::text
                AND s.counterparty_id = t.counterparty_id
          )
        """)  # noqa: S608
    )


# ── Frozen RLS DDL, inlined from ``kag_common.database.rls`` 0.37.0 ──────────
# Same SQL the library emits, kept here so this revision means the same thing
# forever. The predicate casts to match the column type: ``uuid`` columns cast
# ``::uuid``; a ``String`` column compares text-to-text (casting would raise
# "operator does not exist: character varying = uuid").
_TENANT_COLUMN = "tenant_id"
_POLICY_NAME = "tenant_isolation"
_GLOBAL_READ = f"{_POLICY_NAME}_read"
_GLOBAL_INS = f"{_POLICY_NAME}_ins"
_GLOBAL_UPD = f"{_POLICY_NAME}_upd"
_GLOBAL_DEL = f"{_POLICY_NAME}_del"
_ALL_POLICY_NAMES = (_POLICY_NAME, _GLOBAL_READ, _GLOBAL_INS, _GLOBAL_UPD, _GLOBAL_DEL)
_GUC_TEXT = "NULLIF(current_setting('app.current_tenant', true), '')"


def _tenant_table_names(metadata: sa.MetaData) -> list[str]:
    return [t.name for t in metadata.sorted_tables if _TENANT_COLUMN in t.columns]


def _predicate(metadata: sa.MetaData, table: str) -> str:
    col = metadata.tables[table].columns[_TENANT_COLUMN]
    guc = _GUC_TEXT if isinstance(col.type, sa.String) else f"{_GUC_TEXT}::uuid"
    return f"{_TENANT_COLUMN} = {guc}"


def _strict_statements(table: str, predicate: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}",
        f"CREATE POLICY {_POLICY_NAME} ON {table} USING ({predicate}) WITH CHECK ({predicate})",
    ]


def _global_statements(table: str, predicate: str) -> list[str]:
    # Nullable tenant_id ⇒ NULL rows are shared/global (read by all), writes scoped.
    read = f"{_TENANT_COLUMN} IS NULL OR {predicate}"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {_GLOBAL_READ} ON {table}",
        f"DROP POLICY IF EXISTS {_GLOBAL_INS} ON {table}",
        f"DROP POLICY IF EXISTS {_GLOBAL_UPD} ON {table}",
        f"DROP POLICY IF EXISTS {_GLOBAL_DEL} ON {table}",
        f"CREATE POLICY {_GLOBAL_READ} ON {table} FOR SELECT USING ({read})",
        f"CREATE POLICY {_GLOBAL_INS} ON {table} FOR INSERT WITH CHECK ({predicate})",
        f"CREATE POLICY {_GLOBAL_UPD} ON {table} FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})",
        f"CREATE POLICY {_GLOBAL_DEL} ON {table} FOR DELETE USING ({predicate})",
    ]


def _upgrade_statements(metadata: sa.MetaData, *, exclude: set[str]) -> list[str]:
    out: list[str] = []
    for name in _tenant_table_names(metadata):
        if name in exclude:
            continue
        predicate = _predicate(metadata, name)
        out += (
            _global_statements(name, predicate)
            if metadata.tables[name].columns[_TENANT_COLUMN].nullable
            else _strict_statements(name, predicate)
        )
    return out


def _downgrade_statements(metadata: sa.MetaData, *, exclude: set[str]) -> list[str]:
    out: list[str] = []
    for name in _tenant_table_names(metadata):
        if name in exclude:
            continue
        for policy in _ALL_POLICY_NAMES:
            out.append(f"DROP POLICY IF EXISTS {policy} ON {name}")
        out.append(f"ALTER TABLE {name} DISABLE ROW LEVEL SECURITY")
    return out


def downgrade() -> None:
    conn = op.get_bind()

    uuid_tables = [
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT c.table_name FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE c.table_schema = current_schema() AND c.column_name = 'tenant_id' "
                "  AND t.table_type = 'BASE TABLE' AND c.data_type = 'uuid' "
                "  AND c.table_name <> :crosswalk ORDER BY 1"
            ),
            {"crosswalk": CROSSWALK},
        )
    ]
    uuid_tables = _alterable(conn, uuid_tables)
    had_rls = _rls_enabled(conn, uuid_tables)
    exclude = {t for t in _tenant_table_names(Base.metadata) if t not in had_rls}

    for stmt in _downgrade_statements(Base.metadata, exclude=exclude):
        op.execute(stmt)

    for table in uuid_tables:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN tenant_id TYPE VARCHAR(255) USING tenant_id::text"
        )

    # Put the original labels back — but ONLY for the derived mappings.
    #
    # A ``derived`` UUID was minted by this migration and existed nowhere
    # before it, so every row carrying one came from that label and the
    # reversal is unambiguous.
    #
    # A ``registry`` mapping is different, and reversing it corrupts data. The
    # consolidation pointed ``n8n-dev`` at a UUID that ALREADY held 157 rows of
    # its own; nothing in the row distinguishes the 60 that arrived from the
    # label. Measured: reversing it blindly relabels all 217 as ``n8n-dev`` and
    # the tenant's real UUID disappears from three tables entirely.
    #
    # So the downgrade restores the column TYPE and the orphan labels, and
    # deliberately does NOT re-split a tenant that was only ever split by the
    # defect this migration fixed. ``tenant_id_crosswalk`` is dropped last, so
    # read it first if you need the mapping after a downgrade.
    for table in uuid_tables:
        conn.execute(
            sa.text(
                f"UPDATE {table} SET tenant_id = x.legacy_value "  # noqa: S608
                f"FROM {CROSSWALK} x WHERE {table}.tenant_id = x.tenant_uuid::text "
                "AND x.source = 'derived'"
            )
        )

    op.drop_constraint("uq_toc_settings_tenant_project", "toc_settings", type_="unique")
    op.execute("UPDATE toc_settings SET tenant_id = '__global__' WHERE tenant_id IS NULL")
    op.alter_column("toc_settings", "tenant_id", nullable=False)
    op.create_unique_constraint(
        "uq_toc_settings_tenant_project", "toc_settings", ["tenant_id", "project_id"]
    )

    # The text predicates must come back with the TEXT column. They cannot be
    # rendered from ``Base.metadata`` here: the models ship as ``Uuid``, so the
    # helper would emit ``…::uuid`` against a varchar column and every query on
    # a downgraded database would die with "operator does not exist:
    # character varying = uuid". Rebuilt explicitly, mirroring
    # ``2026_07_09_0031:33-35``, which is the shape these policies had before
    # this migration.
    guc = "NULLIF(current_setting('app.current_tenant', true), '')"
    predicate = f"tenant_id = {guc}"
    for table in sorted(had_rls):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )

    op.drop_table(CROSSWALK)
