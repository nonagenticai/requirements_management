# Vendored build inputs

`scripts/build_pm_catalogue_defaults.py` reads these copies, never a sibling checkout
(the local `n8n_custom_nodes` clone was found stale and built a wrong dataset twice).
Each file is byte-identical to its source blob (`trigger_registry.md` below its `<!-- ... -->`
header block — the header carries, on a line of its own, the command that checks it, and states
the line to start from), except the 4 py2rust `rewrite_*` card types listed below.

| file | source | commit | blob |
|---|---|---|---|
| `trigger_registry.md` | `nonagenticai/n8n_custom_nodes` `AA_IMPLEMENTATION/trigger_registry.md` | d469ca1c6 (2026-10-03) | 314b21f5 |
| `landing_predicates.json` | `nonagenticai/n8n_custom_nodes` `AA_IMPLEMENTATION/landing_predicates.json` | e21ee10f0 (2026-10-06) | 7ce8babe |

⚠️ `trigger_registry.md` is **no longer purely the archived blob**: it carries 4 card types added
for the py2rust rewrite (RS-D4, George 2026-10-25) — `rewrite_plan`, `rewrite_slice`,
`rewrite_parity_qa`, `rewrite_cutover_readiness` — each marked in its body line. `n8n_custom_nodes`
is ARCHIVED, so the registry's live home is the design copy
`nonagenticai/n8n-workflows` `design/choices/v4/vendor/trigger_registry.md`; this file stays
byte-identical to it below the header (the header carries the check command, on its own line so it
copy-pastes as-is — it diffs against the design repo's **committed** `origin/main` blob, never a
working tree, and it is green now that n8n-workflows#63 is merged). A new card type is added to
BOTH in the same unit of work, never to one.

Refresh: `git -C ../n8n_custom_nodes fetch origin main && git -C ../n8n_custom_nodes show origin/main:<path>`
for `landing_predicates.json` (and the unchanged part of `trigger_registry.md`), then update this
table, re-run the builder and its `--check`.

Not vendored (and why):

- `n8n-designs/design/choices/v4/capabilities/*.yaml` — **every** file (11 at 2026-11-01; R228
  says every one, not a subset — the builder globs the directory)
  (card type inputs/outputs/acceptance, R193) — read live from the design repo at
  `~/Movies/n8n-designs`, the design's only home, via `--v4`.
- `coding_card_types.json` (n8n_custom_nodes @ bd97bbede) and fr `field-groups.ts` — transcribed
  into constants in the builder, each citing its source; the builder does not read those files.
