"""Split the reconciler's failure streak in two (transient vs quarantining).

``conflict_reconcile_failure.consecutive_failures`` counted EVERY failure kind,
while the quarantine gate separately exempted a row whose ``last_failure_kind``
was transient. One counter cannot express two streaks, and that produced three
proven defects at once:

* transient, transient, permanent at a threshold of 3 QUARANTINED a healthy
  procedure — two provider 429s plus one real error;
* a procedure already quarantined on three permanent failures was RELEASED by a
  single transient re-probe, so a broken procedure evaded quarantine forever by
  occasionally flaking;
* a DETERMINISTIC transient (a body that reliably exhausts worker_management's
  output-retry budget — the token-limit 422 issue #108 was filed on) could never
  be quarantined at all, costing a full 12000-token call every cycle forever.

``consecutive_transient_failures`` counts the non-quarantining kinds
(``transient_llm_error``, ``upstream_payload_incomplete``) and
``consecutive_failures`` now counts only the quarantining ones
(``permanent_llm_error``, ``incomplete_coverage``). Each streak gets its own
threshold, the quarantine gate no longer reads ``last_failure_kind`` at all, and
all three defects become impossible rather than separately guarded.

Backfill is deliberately ZERO, not a split of the existing value: an existing
row's ``consecutive_failures`` was accumulated under the old mixed semantics, so
any apportionment would be invented. Starting the new counter at 0 can only
delay a transient quarantine by at most one threshold's worth of cycles, which
is the safe direction. Measured at cutover: the table held 0 rows in prod.

The ROLLING-UPDATE window needs its own answer, and "0 rows at cutover" is not
it. That measurement is about rows written BEFORE this migration; during the
rollout the OLD pod is still serving and still writing. Its upsert increments
``consecutive_failures`` for EVERY kind, 429s included, and it relied on the
``last_failure_kind``-transient exemption in the quarantine gate to neutralise
them. The new gate does not read that field at all, so a provider blip
concurrent with the rollout can leave a perfectly healthy procedure sitting at
``consecutive_failures >= 3`` that the new pod quarantines on sight for up to
``conflict_reconcile_quarantine_retry_s`` (86400s) each — a coverage bug, the
worse of the two failure modes, because a quarantined procedure reports its
tenant clean. ``upgrade()`` therefore applies the old exemption once, as data.

⚠️ Be clear about what that does NOT cover: it is a single statement at
migration time, so it neutralises rows the old pod wrote BEFORE it ran, not the
ones it writes in the seconds AFTER, while the old ReplicaSet is still draining.
That residual is accepted rather than fixed, on two bounds: the window is the
rollout itself (one reconcile cycle is 900s, so the old pod usually writes
nothing at all inside it), and the table held 0 rows at cutover, so the
population able to be damaged is whatever a single in-flight cycle produces. The
exit for anything that does slip through is already built: a re-probe every
86400s, and an immediate release the moment the procedure's body changes.

Forward-only — pre-prod, no rollback shims.

Revision ID: 2026_08_24_0041
Revises: 2026_08_19_0040
Create Date: 2026-08-24
"""

import sqlalchemy as sa

from alembic import op

revision = "2026_08_24_0041"
down_revision = "2026_08_19_0040"
branch_labels = None
depends_on = None

_TABLE = "conflict_reconcile_failure"
_COLUMN = "consecutive_transient_failures"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
    )
    # Apply the OLD kind-exemption once, as data — see the rolling-update
    # paragraph in the module docstring. A row the old pod left at
    # `consecutive_failures >= 3` purely because a provider flapped was harmless
    # only while the gate exempted it on `last_failure_kind`; the new gate does
    # not read that field, so the exemption has to be spent here or it is lost.
    #
    # The kind is a LITERAL, deliberately. A migration is a statement about the
    # database as it was at this revision, so it must not import
    # `models.conflict_reconcile_failure.FAILURE_KIND_TRANSIENT`: renaming that
    # constant's VALUE later would silently retro-change what this already-run
    # migration meant, and re-running it on a fresh database would then match
    # nothing.
    #
    # `upstream_payload_incomplete` — the other non-quarantining kind — is
    # deliberately NOT included, because a row last written by it cannot
    # quarantine anything under the new code even at a high count: the
    # steps-missing skip in `conflict_reconciler` sits ABOVE the quarantine
    # gate, and if the steps do arrive the body hash cannot equal the stored
    # raw-payload hash, so `_is_quarantined` fails its fingerprint test and
    # returns False. Zeroing it would be a no-op dressed as a fix.
    op.execute(
        f"UPDATE {_TABLE} SET consecutive_failures = 0 "
        "WHERE last_failure_kind = 'transient_llm_error'"
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
