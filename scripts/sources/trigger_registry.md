<!-- VENDORED copy of nonagenticai/n8n_custom_nodes AA_IMPLEMENTATION/trigger_registry.md
     at commit d469ca1c6 (2026-10-03; unchanged on origin/main 08db8dadd), blob 314b21f5ed2a38a406029fd7ae59b6abe3bdd85b,
     PLUS the 4 py2rust rewrite_* card types added 2026-10-26 (RS-D4, George 2026-10-25) — each
     body line marked "(added 2026-10-26, py2rust RS-D4, George 10-25)". Everything else below the
     header is byte-identical to that blob. It is kept byte-identical to the COMMITTED design copy
     nonagenticai/n8n-workflows design/choices/v4/vendor/trigger_registry.md, the registry's live home
     now that n8n_custom_nodes is archived. It needs n8n-workflows#63 merged. The next line is the
     check: it copies and pastes as-is (the closing marker sits on its own line for that reason);
     git -C ~/Movies/n8n-designs fetch -q origin main && diff <(tail -n +11 scripts/sources/trigger_registry.md) <(git -C ~/Movies/n8n-designs show origin/main:design/choices/v4/vendor/trigger_registry.md)
-->
Webhook Trigger Register

(George's document. Rewritten 2026-09-30 (design-cmm5) against `AA_IMPLEMENTATION/TENANCY_AND_IMPROVEMENT.md`: §9 event
model, §10 WIP limit per project (George 2026-09-30), §11 the CMM-5 loop, §12 capabilities, §12a no agency. Where a line of George's is changed,
his original line is quoted verbatim and the "Implementation:" under it says how it is realised. Everything else of his is kept
as written; additions are marked.)

George's text:
> Implement stopwatch 2 minutes in DOING auto send to blocked. Prefetch all required data in READY, execute all slow validation in QA. Split every card that gets moved to QA based on stopwatch. 

Implementation (ruled by George 2026-09-30, Q1 — his 2-minute budget and "split … based on stopwatch" wording above are
superseded by that ruling; the quote stays as he wrote it):
- The stopwatch is a SAFETY NET AGAINST SYSTEM/APP FAULTS ONLY. It is not a scope, quality or split signal.
- It is a PER-CARD DEADLINE (an event-armed timer). The card's own DOING-entry event arms one deadline for THAT card only:
  `card_deadline(card_id, doing_entry_id, fires_at = entry + 10 min)`. It is not a clock sweep: nothing scans the board, and
  clock sweeps that move cards stay REMOVE (see "Trigger table"). It is the ONE sanctioned per-card timer.
- Budget: a flat 10 minutes per card, for every card type. No per-type budget, no learning from measured durations.
- It fires at most once for that DOING entry and emits `stopwatch_expired` (TI §11.3 S7, a system-fault sensor) → router →
  DOING→BLOCKED with a system-fault reason (`block_reason` `stopwatch_expired: system fault`). No split, no scope judgement,
  no child cards. DOING-exit cancels it; a stale deadline (the card left or re-entered DOING) cannot fire.
- Splitting is unrelated to the stopwatch: whether to split is computed only from the INVEST S letter (see "Definition of
  Ready"). QA entry never splits a card on elapsed time, and no timeout drives a split.
- Not built: fr `stopwatch-tick` and the watchdog stopwatch census are deleted; nothing fires on a deadline today (TI §9.6).
  Owner: movies-70 (TI §16 row 11).


Project type catalogue
George's text:
> program - project of projects managing the dependencies between the projects and the overal direction
> meta_prompt - project about the continuous and incremental improvement of prompts used in projects
> meta_procedure - project about the continuous and incremental improvement of procedures used in projects
> kaizen - project about the continuous and incremental improvement of workflows used in the system
> it - project that starts with receipt of a github repo url (and optional credentials) and ends with a pull request. Scope Levels: multi_repo, repo, app, module, component, file, class, function

Implementation (tenant per project type, enforced at create by fr `PROJECT_TYPE_TENANT`, fr backend/src/routes/projects.ts:263-272):
- program, it — EXECUTION `…0003` (or a customer tenant). test, dev and demo projects are also `…0003`.
- meta_prompt — PROMPT-IMPROVEMENT `…0004`; meta_procedure — PROCEDURE-IMPROVEMENT `…0005`; kaizen — WORKFLOW-IMPROVEMENT `…0002`.
- `…0001` owns no execution or stream project type: it holds the PUBLISHED system assets and does PUBLISH/DISCARD (TI §2.1,
  §11.4).
- Principles / requirements / decisions (ruled by George 2026-09-30, Q3; no new project type, no sixth tenant): SYSTEM-level
  P/R/D (used by the workflow/procedure/prompt improvement projects) are owned by George/Claude in `…0001` and kept relatively
  stable; a change to one is a rare, deliberate improvement card in the owning stream above, or a George/Claude super-admin
  action. PROJECT-level P/R/D are owned by the project owner, in that project's own tenant and project.


Board list catalogue
TODO - prioritized backlog
READY - completed preparations for execution (data gathering, preprocessing, context-engineering, procedure-retrieval)
DOING - execution of procedure on context
QA - verification of alignment of execution to request and testing/validation of execution output
BLOCKED - behaviors to unblock a card (request input from human to: start project to create certain assets, make a decision, request additional input, in order to move the card back to READY list)
DONE - final validation (testing/inspection/evaluation), publishing (commit or for non-code publish to artifact_service)

(Addition.) Every move between these lists is caused by an EVENT (a Planka card event on the router webhook, or the completion /
failure envelope of the pattern working on that card), computed from the state recorded when the event arrives. No sweep, poll
or tick moves a card (TI §9.1-9.2). The per-card deadline above is armed by an event and acts on one card. See "Trigger table".

Definition of Ready (INVEST) — called only by the UNARMED READY pull (2026-09-25)
Readying pulls from TODO and moves the cards that meet the definition of ready. Ready = INVEST (Independent, Negotiable,
Valuable, Estimable, Small, Testable), COMPUTED from data the card already carries — no model call.
- Routine: `AA Routine - ready.invest` (workflows/routines/r.ready.invest.json), COMPILED from
  workflow-specs/r.ready.invest.spec.json. Input { ctx, params: { card_id } }. Reads kanban.card.get + kanban.board.get; writes nothing.
- Output (envelope data): { ready, letters: { I, N, V, E, S, T: { status, pass, evidence } }, failing, unknown, not_computed, split_hint, policy }.
  status is pass | fail | unknown | not_computed. ready is true ONLY when I, E, S and T all pass. Unreadable evidence ⇒ that
  letter unknown ⇒ ready:false (fail closed). An unreadable card or board ⇒ a fail envelope (run.halted), never ready:true.
  split_hint is true when S fails.
- I  Independent — every id in Relations.requires + Relations.child_cards is a card on this board in the DONE list. DONE is the FIRST
     list whose trimmed, case-insensitive name is in done_list_names — the list todo-pickup :: Compute Deps resolves against
     (lists.find), so the two can never disagree about which list is DONE. First matching rung wins: an unparseable Relations value
     ⇒ unknown · the card lists ITSELF (malformed; reported as self_dependency, never silently dropped) ⇒ unknown · no dependency ⇒
     pass · requires_board names another board ⇒ unknown · the board has no DONE list ⇒ unknown · a dependency is open ⇒ fail · a
     dependency is not on the board, or sits on a LATER DONE-named list (todo-pickup's doneNamedUnresolved) ⇒ unknown · else pass.
- E  Estimable — Metadata.card_type has a FIXED doing sequence: <track>.<card_type>.doing.activity_sequence_override in
     $env.AA_CUSTOMISATIONS_JSON (workflows/customisations.json). A row without an override (chain-select picks at runtime) or no
     row ⇒ fail; card_type absent or the env unreadable ⇒ unknown.
- S  Small — acceptance-criteria count ≤ max_acceptance_criteria AND description chars (machine <!-- aa:*:start/end --> regions
     excluded) ≤ max_description_chars. Nothing else: Metadata.scope_level is a PROJECT-scope field (live 2026-09-25, 1,338
     cards: module 757 / repo 493 / component 49 / multi_repo 34 / app 2), not a card size, and is NOT read. Criteria are the
     template's criterion_N fields (live: every card; 0 carry a single acceptance_criteria field); a single acceptance_criteria
     field counts only as a JSON array of non-empty strings — any other content ⇒ criteria unreadable ⇒ S and T unknown.
- T  Testable — ≥ min_checkable_criteria acceptance criteria in a CHECKABLE form: Gherkin (when … then, given optional), a
     measurable bound (a number with a comparator or a unit), a code/file/endpoint reference (`code`, a path.ext, METHOD /path,
     /path, HTTP status), or a command/test reference (npm run …, node x.mjs, pytest …, test_x, x.test.ts). A bare verb is NOT
     checkable: "Users can log in", "It fails gracefully", "the solution contains no bugs", "Documentation exists" all fail T.
- N  Negotiable, V Valuable — not_computed: no card field carries them. Reported, never scored, never block ready.
- Thresholds: $env.AA_READY_INVEST_POLICY, a JSON object; every key optional, an unknown key or a bad type ⇒ every letter unknown.
  Defaults: {"max_acceptance_criteria":7,"max_description_chars":4000,"min_checkable_criteria":1,"done_list_names":["DONE"]}.
  small_scope_levels is RETIRED: a policy still carrying it is an unknown key ⇒ every letter unknown.
- Caller: ONLY AA Pattern - ready.pull (workflows/patterns/p.ready.pull.json, see "READY pull" below), which is UNARMED — its
  it|architecture_task|ready registry row has no exec_v2_rollout key, so no production card reaches it. No cell, router or
  ready-prefetch node calls it. Its orphans baseline entry was removed with that caller; it is unarmed_only now. Overlaps to settle BEFORE ARMING: todo-pickup :: Compute Deps already
  holds a card in TODO on open child_cards/requires (I; it also follows requires_board to a foreign board, which this routine reports
  unknown; a self-dependency it counts as an open blocker — a permanent hold — where this routine reports it as malformed), and ready-prefetch's LLM split-judge already judges scope (S). Wiring means ONE owner for each.
- Follow-ups: V could trace to Requirements group / Metadata.project_id / fr project_deliverables (fr migration 127, no n8n op today);
  N has no data source — either an infer enum atomic or leave it not_computed. Proof: scripts/tests/test-ready-invest.mjs.
- (Addition, 2026-09-30) Target caller: the TODO→READY evaluation, fired by `card_created` in TODO and by `card_done` of a card
  this one requires or has as a child (TI §9.2).
- (Addition, 2026-09-30) The split decision is COMPUTED, never judged (TI §12a). WHETHER to split = the S letter fails
  (`split_hint`), and nothing else; `stopwatch_expired` never triggers a split (George 2026-09-30, Q1). HOW to split is content: the model writes the children into a
  declared `proposed_children` field and chooses nothing. Only when there are two or more split plans does `a.infer.select_option`
  return ONE option id from the offered set (checked by `a.compute.options.validateChoice`; agreement mode per `r.select.agreed`,
  owned by movies-70). No agreement ⇒ nothing is split and the card goes to BLOCKED as a measured event. The ready-prefetch LLM
  split-judge (workflows/patterns/ready-prefetch.json, worker-split-judge / Parse Split, a free boolean plus free children) is
  REMOVED by this rule; one split producer (D4).

Card Type Catalogue
Card Type Catalogue Categories:
program_management - card types that deal with program management
project_management - card types that deal with project management
change_management - card types that deal with change management
meta_prompt - card types for prompt improvement in flows
meta_procedure - card types for procedure improvements for agents
kaizen - card types for flow improvements
workflow_engineering - card types that deal with changing the workflow itself (a kata's work; added 2026-11-01)
(Addition: meta_prompt cards are minted only into `…0004`, meta_procedure only into `…0005`, kaizen only into `…0002`.
Improvement cards are minted only by a sensor from a measured failure event, into the stream tenant of the attributed asset
kind — TI §11.4. Execution patterns never mint.)


Program Management Card Type Catalogue
program_start_request_analysis - analyzes the program brief of the program start request from the user or other source
program_governance - Defining, reviewing, and enforcing program-level rules, standards, and compliance

Project Management Type Catalogue
project_start_request_analysis - analyzes the project brief of the project start request from the user or other source
project_setup - special card for setting up project and confirming setup was successful
project_closure - special card for closing projects
feature_start_request_analysis - analyzes a feature start request against existing requirements for scope, dependencies and delivery constraints (added 2026-09-30 from the roster)

Change Management Card Type Catalogue
dependency_mapping - Identifying, documenting, and managing inter-project dependencies.
stakeholder_comms - Planning and executing communication strategies with various stakeholders.
release_planning - Defining preparing releases
project_risk_analysis - Identifying, evaluating, and mitigating risks at the program level.
future_state_analysis -  Identifying discrepancies between current state and strategic goals.
change_principle_development - Defining overarching program principles and guidelines.
change_requirement_development - Capturing high-level strategic requirements.
change_decision_development - Documenting key program-level strategic decisions.
rewrite_cutover_readiness - collect the G4 cutover evidence for a language rewrite: parity results true on the pinned source SHAs and a data-copy plan; the cutover itself is a session action; upstream rewrite_parity_qa (added 2026-10-26, py2rust RS-D4, George 10-25)

Prompt Improvement Card Type Catalogue
prompt_performanc_statistics_analysis - analyze failure patterns for structured output or other failures
prompt_improvement - improving prompts based on analysis
prompt_library_consistency_management - review and apply recent changes to specific prompts to other similar prompts
    Implementation: only AFTER a prompt change has been PUBLISHED by its own measured canary (TI §11.4): find similar prompts and
    mint one child prompt_improvement card per candidate. Never bulk-apply; each child is promoted only by its own measured canary.

Procedure Imrprovement Card Type Catalogue
procedure_performance_statistics_analysis - analyze performance of procedures
procedure_improvement - improving procedures based on analysis
procedure_library_consistency_management - review and apply recent changes to specific prompts to other similar procedures
    Implementation: read as "recent changes to specific procedures" (George's line says "prompts"; read as a slip of the pen, unsettled until he confirms — not one of the Q1–Q4 questions). Only
    AFTER a procedure change has been PUBLISHED by its own measured canary (TI §11.4): find similar procedures and mint one child
    procedure_improvement card per candidate. Never bulk-apply; each child is promoted only by its own measured canary.
governor_performance_analysis - analyze whether acting on a governor version actually improved downstream outcomes (added 2026-09-30 from the roster)


Kaizen Card Type Catalogue
workflow_statistics_analysis - find workflows with weakest performance
workflow_performance_analysis - find root cause of workflow performance
workflow_redesign - design improved part of a workflow based on analysis
workflow_pattern_catalogue_improvement - improve patterns in the workflow_pattern_catalogue to improve generation of workflows in the future
workflow_development - develop improved workflow

Asset Library Statistics Card Type Catalogue (added 2026-09-30 from the roster; system-level P/R/D live in `…0001` and change rarely and deliberately, George 2026-09-30, Q3)
decision_statistics_analysis - surface near-duplicate, contradictory or low-quality decisions in the decision library
principle_statistics_analysis - surface near-duplicate, contradictory or low-quality principles in the principle library
requirement_statistics_analysis - surface near-duplicate, contradictory or low-quality requirements in the requirement library



Research Card Type Catalogue
research_spike - gain knowledge on a given topic
prototype_experiment - minimal prototype development to explore research_spike results
feasibility_study - study based on prototype_experiment and research_spike outcomes to decide feasibility for implementation into the repo
domain_analysis - study to prepare for architecture, design and/or feature development
technology_landscape_survey - study to state-of-the-art technologies, tools, libraries, frameworks, design patterns relevant to a planned project or change — for decision support, comparison, and selection.


Analysis Card Type Catalogue
bug_analysis - uses tools to find bugs and analyze root cause
security_analysis - uses tools to find security flaws
code_quality_analysis - uses tools to evaluate code quality
structure_analysis - uses tools to evaluate the structure of the codebase
consistency_analysis - uses tools to evaluate consistency of the codebase
gap_analysis - delta analysis between current and future states
plateau_planning_analysis - analysis of intermediate stable states between current and future state
change_risk_analysis - risk analysis scoped to the risk of changes planned to be made
change_impact_analysis - analysis to the magnitude and complexity of achieving the future state given the current state
rewrite_plan - draft the target architecture and an ordered slice list for a language rewrite from the source repos at pinned SHAs; the user confirms the draft (R212) (added 2026-10-26, py2rust RS-D4, George 10-25)

Architecture Card Type Catalogue
architecture_spike - defines the objective of the spike and a directive for architecture_epics to follow
architecture_epic - defines the objective of the epic and a directive for the architecture_tasks to follow
architecture_task - defines the task and how it will be evaluated and a directive for architecture_subtasks to follow
architecture_subtask - applies the directive to the work to be executed
principle_development - creation, review, and updating
requirement_development - creation, review, and updating
choice_option_development - creation, review, and updating
decision_development - capturing, review, and revision
architecture_baseline_harvest - populate the baseline architecture model from the running system and bind each element to the code that realises it (added 2026-09-30 from the roster)

Design Card Type Catalogue
diagram_design - creation of design diagrams for getting approval on a design before development can start
narrative_design - creation of narrative for the design to communicate design intent
design_packaging - bundle design and architecture and analysis artifacts in order to present the design


Testing Card Type Catalogue
testing_strategy - sets the objectives, principles, goals and structure for testing
testing_plan - defines how tests will be executed and what the evaluation process is
test_development - creation of or updating tests
testing_execution - running tests
testing_evaluation - evaluating test results
test_process_evaluation - evaluating of the test procedures and organization
verification_test - runs coding-node import-smoke + pytest against the upgraded container after a development_chore opens a draft PR; deterministic all_ok gate publishes the PR at DONE or blocks it
rewrite_parity_qa - prove a rewrite equals its sources at pinned SHAs: schema-diff equal, openapi-diff equal against the union of the source specs, behaviour replay/compare equal, all computed with no judgement; upstream rewrite_slice (added 2026-10-26, py2rust RS-D4, George 10-25)

BDDTDD Card Type Catalogue
bdd_test_development - gherkin defined test use cases and acceptance criteria
bdd_test_evaluation - evaluation of quality and alignment of bdd tests with project objectives and the subject of the test
bdd_test_improvement - improvement of the bdd tests based on the evaluation
tdd_test_development - test creation (prior to development)
tdd_test_evaluation - evaluation of created tests
tdd_test_improvement - improvement of tests based on evaluation

Development Card Type Catalogue
development_poc - proof of concept development
development_bug_fix - can be single file or multiple file bug analysis for the same bug
development_feature_new - development of a new feature
development_feature_update - enhancement of an existing feature
development_security_patch - security patch implementation
development_chore - uncategorized coding job
development_iac_security_patch - fix an infrastructure-as-code misconfiguration and open a pull request (added 2026-09-30 from the roster)
development_source_security_patch - fix a source-code security finding and open a pull request (added 2026-09-30 from the roster)
rewrite_slice - Rust code for one slice of the confirmed rewrite plan in the target repo, opened as a PR; done when the unified workspace test run (pytest + nextest) is green and openapi-diff shows the slice's routes present and equal; upstream rewrite_plan (added 2026-10-26, py2rust RS-D4, George 10-25)

Refactoring Card Type Catalogue
re_engineering - full fletched reconsideration of existing repo
architecture_refactoring - architectural refactoring of repo
decoupling_refactoring - architectural refactoring scoped to coupling of repo
library_refactoring - refactoring of library choices and library version configurations
code_smell_refactoring - refactoring scoped to finding and resolving code smells
optimization_refactoring - refactoring scoped to performance optimization
function_class_reallocation - refactoring scoped to reconfiguring function class layout of the repo structure
dependency_refactoring - refactoring scoped to resolving, reducing or removing dependencies

NOTE (2026-09-30, catalogue vs roster): the 8 types that were in the roster (AA_IMPLEMENTATION/card_type_descriptions.json) and
not here are added above, marked "added 2026-09-30 from the roster"; this catalogue now names 84 types, as the roster does.
Name divergence, pending vocabulary ruling (vocabulary single-sourcing is UNSETTLED): George's `prompt_performanc_statistics_analysis`
and `project_risk_analysis` appear in the roster as `prompt_performance_statistics_analysis` and `program_risk_analysis`. Neither
side is renamed here; today the roster resolves them through its `trigger_registry_aliases` block (card_type_descriptions.json:25-28),
which stays. The category list above does not yet name the Research … Refactoring and Asset Library Statistics sections; that
too waits for the vocabulary ruling.

Workflow Engineering Card Type Catalogue
wf_observe - writes a five-line observation from the evidence: what, where, when, how often
wf_probe - checks /health and Prometheus up for every node-module app the failing flow uses
wf_root_cause - chooses one root-cause class and quotes the evidence line that proves it
wf_andon_scope - decides whether a defect in a shared machine reaches other running projects
wf_change - applies one op list to a copy of the failing flow, proves it by replay, publishes it and releases the cord
(Addition 2026-11-01, Kata WP4 — George 2026-11-01 K3/K11/K17/K19, design/choices/v4/kata/wp4-grasp.html:
the `workflow_engineering` capability is the work a `kata` project does. Its card types are placed on the
`kata` project type ONLY — they are never placed on `it`, the explicit exception to R228. Execution phase
(Kata WP5, George 2026-11-03, design/choices/v4/kata/wp5-change-replay.html): ONE card type, wf_change, run on
kata-n8n. wf_design, wf_test, wf_publish and wf_watch do not exist (design = wf_change.READY's op list, test =
DOING's replay, publish = DONE); wf_yokoten and wf_app_restart are deferred and are not card types.)

Board list trigger register (given project type + card type + list -> flow)
(Rows are not kept here. Two separate paths resolve card_type × list today. Legacy (live): `scripts/lib/card-plan.mjs` (#1638, written out by `scripts/gen-customisations.mjs`) computes a sequence of legacy activity patterns into `workflows/customisations.json`. v2: `scripts/pattern-registry.json` names one L2 pattern, used only for keys armed in `AA_IMPLEMENTATION/exec_v2_rollout.json`. Layer model: `docs/redesign/design/exec-design.md` §1.1.)

WIP limits (pull into DOING) — ⛔ NOT ARMED
George's text (2026-09-28, session cee35825-83ae-417c-aa45-30f04e1d2067, 08:21:38Z, quoted in the r11 design
~/.claude/plans/design-cmm5-scratch/r11/WIP-PER-PROJECT-DESIGN.md §1):
> Based on the WIP limit set on the project/tenant/system (3 potential levels to prevent system-collapse) the card get pulled into DOING

George's text (2026-09-30):
> wip was envisioned per project, not sure what the 14 slots is about. i think we need some kind of way to manage the pgx inference to not overload the vllm instance, but other than that i do not think we need cross-tenant wip enforcement.

Implementation (George 2026-09-30; supersedes the 3-level gate and the Q2 per-tenant budgets / derived system 14):
- ONE level: per project. Cards are pulled into DOING only while their project's WIP limit allows it. No system level, no
  tenant level.
- The limit is held by an ATOMIC CLAIM in fr, not by a board read: `wip_slot` (fr migration 138_wip_slot.sql;
  backend/src/routes/webhooks/wip-slot.ts). `pg_advisory_xact_lock` serialises count-then-insert (:493/:626/:690) and
  `INSERT … ON CONFLICT (card_id) WHERE released_at IS NULL DO NOTHING` (:529) gives one winner. The check-then-act race is
  therefore SOLVED; a loser gets granted:false and nothing moves. Counts come from open slot rows, not from boards the Planka token
  can see, so the visibility limit is closed too. Slots have no TTL; they are freed by `release` (DOING-exit) or `reconcile`.
- Project = the fr project (one Planka board). Tenant of a card: its `keycloak_tenant_id`, else the tenant of the fr project that
  owns the board; neither resolves ⇒ refused (fail closed). The tenant is the claim's SCOPE and AUTH key (JWT tenant pin,
  release-by-slot-id, reconcile), not a limit (TI §10).
- Limit: `AA_WIP_LIMITS = {"project":{"default":1}}`; an exception is a per-project override keyed by the project id
  (= `wip_slot.project_id`): `{"project":{"default":1,"<project_id>":N}}`. No per-project-type limit (TI §10).
- Enforcer: fr (the claim above). Mirror: n8n `r.wip-gate`, `r.wip-select`, `r.wip-claim`, project level only. Once fr#1315 is
  deployed (fr main 6f78eba7 still requires system/tenant), a config still carrying `system` or `tenant` will be refused by
  fr's strict schema (fail closed). Live 2026-09-30 (A5): the n8n ConfigMap still holds
  the retired `{"system":6,"tenant":{…},"project":{"default":1}}` (gitops clusters/strato/services/n8n/configmap.yaml:230) and fr has
  none; gitops#4279 (system 14) is superseded (TI §16 row 12).
- Deploy order (TI §10; replaces "ncn → fr → gitops"), each step fail-closed or stricter: (1) gitops#4281 first — the old
  r.wip-gate reads {"project":{"default":1}} with its system 4 / tenant 4 fallbacks (stricter, still works), fr refuses as
  today; (2) then the ncn project-only r.wip-* (the new gate refuses system/tenant keys, so it lands after gitops);
  (3) then fr#1315.
- pgx is NOT protected by a WIP level. Its bound is the inference-router's concurrency cap, 16 per replica × 2 = 32 = vLLM
  `--max-num-seqs=32`; no pgx/vLLM change is needed. The retry-storm fix lives in inference-router (slot-wait timeout → 503 +
  Retry-After) and worker_management (`queue_rejected` skips the recreate ladder) (George 2026-09-30 follow-up; TI §10).
- `AA Routine - wip-gate` (workflows/routines/r.wip-gate.json, compiled from workflow-specs/r.wip-gate.spec.json) STAYS — it is not
  deleted (George 2026-09-30 WIP ruling) — and evaluates the project level only. It counts DOING cards from Planka board reads, so
  its own check-then-act race and visibility limit stay open; it is called only by the UNARMED `AA Pattern - ready.pull`.
  Fail-closed codes: invalid_input, config_invalid, input_mismatch, count_unavailable. Tests: scripts/tests/test-wip-gate.mjs,
  tests/scenarios/routines/r.wip-gate/.
- Repo lease: taken in the same claim as the WIP slot; a lease-busy card waits in READY (`waiting_on: repo:<url>`) and is retried
  first when that lease is released (TI §10).
- Arming any of this makes the conveyor run: it requires George's explicit go (conveyor quiesce ruling) and is not part of this
  design change.

READY pull (READY → DOING) — ⛔ UNARMED (2026-09-25); one of TWO READY → DOING paths, both project-level: this one
(`ready.pull`, via `r.wip-gate`) and the WIP pull below (via `r.wip-select` / `r.wip-claim`)
- Pattern: `AA Pattern - ready.pull` (L2, `workflows/patterns/p.ready.pull.json`, COMPILED from `workflow-specs/p.ready.pull.spec.json`). Input `{card_id, board_id, project_id, tenant_id}`.
- Calls `AA Routine - ready.invest` {card_id} → `AA Routine - wip-gate` {card_id, board_id, project_id, tenant_id} → ONE Route set computes route ∈ {doing, not_ready, hold_wip, halt} → ONE switch.
- doing ⇔ invest ok:true && data.ready === true AND wip ok:true && data.allowed === true → `AA Routine - card-move-to-list` DOING. Nothing else ever moves a card to DOING (fail closed).
- not_ready (invest ok, ready:false) → card-move-to-list TODO, then `AA Routine - comment-write` naming the failing and unknown INVEST letters, plus "recommend split" when split_hint is set. The split itself is NOT done here (D4: one split producer).
- hold_wip (wip ok, allowed:false) → no move, no comment; data.level_tripped is in the envelope, and its only values are now `project` (the project limit tripped) or null (not tripped, or a config / input / count refusal) — there is no system or tenant level. halt (a failed or halted call, or a ready / allowed that is not a boolean) → no move, the error is returned.
- UNARMED: the it|architecture_task|ready row in scripts/pattern-registry.json has no AA_IMPLEMENTATION/exec_v2_rollout.json key; cells are byte-equal. Arming guard + tests: `scripts/tests/test-ready-pull-pattern.mjs`.
- Before arming: close the WIP gate's check-then-act race and visibility limits (above), and settle the todo-pickup / ready-prefetch overlaps (Definition of Ready, above).
- Target: its ready.invest call moves to the TODO→READY evaluation; the WIP pull (`s.wip.pull`) is the ONLY way into DOING.
  ⚠️ Open: ready.pull (unarmed, George 12:31Z lift GO set) can also move READY→DOING through r.wip-gate; both are gated per
  project.
  Correction 2026-09-30 (WIP per project ruling): ready.pull and its `r.wip-gate` call are NOT deleted or replaced; the gate stays,
  project level only (see "WIP limits" above). The legacy ungated READY→DOING move in workflows/patterns/ready-prefetch.json
  (node `Move To DOING`, :1303) is deleted (TI §10).

WIP pull (READY → DOING, event-driven) — ⛔ NOT ARMED: dark behind `AA_READY_PULL_ENABLED` (default off) AND an UNARMED entry (B1, 2026-09-28)
- Layered and COMPILED (no raw HTTP to fr): `AA Entry - wip-pull` (`workflows/entries/e.wip-pull.json`, L1e, webhook `aa-wip-pull`, onReceived; auth header `x-aa-wip-pull-token` vs `AA_ROUTER_WEBHOOK_TOKEN`, SHA256 constant-time) → `AA Step - wip.pull` (`workflows/steps/s.wip.pull.json`, L4s) → `AA Routine - wip-census` / `wip-sync` / `wip-select` / `wip-claim` / `wip-move` (`workflows/routines/r.wip-*.json`), all compiled from `workflow-specs/`. fr is reached ONLY through the typed atomics `a.fr.wipSlot.{claim,release,reconcile,count}` (n8n-nodes-fr#26) with a minted `system-execution-operator` bearer (the only `aa-orchestrator` holder) and `X-Tenant-ID` = the card's tenant.
- Steps (no LLM, no timer, no judge): census (flag gate FIRST — off ⇒ ends before any read; `as_of` BEFORE the Planka read; list names resolved on the board) → trigger iff the card LEFT DOING or ENTERED DONE / BLOCKED / READY (router) or any ready-prefetch call → sync (release the card that left DOING BY ITS STORED `slot_id` — `release_without_slot_id` otherwise, fr never asked; reconcile every tenant, ids as digit strings, `allow_empty` only on a successful empty read) → select (fr `count` per scope; the oldest READY candidate whose project has room — project level only, George 2026-09-30; fr limits unset ⇒ nothing, reason `config`) → claim ONE slot → record `slot_id` on the card (Runtime State `wip_slot_id`) → RE-READ the card immediately before the move: still the same card on the same board in the READY list it was selected from ⇒ READY → DOING; otherwise (another actor moved it meanwhile) it is NOT moved and the claimed slot is RELEASED by its recorded `slot_id` (reason `card_moved_before_pull`, route `card_moved_before_pull`; a release fr does not confirm FAILS the run — a slot is never dropped silently). NOT GRANTED (card stays READY, nothing else happens): `granted:false` (project / config), 403 (`wip_slot_*` code), 409 (`conflict`).
- Triggers — EVENTS ONLY, never a timer: a card LEAVES DOING (`doing>qa`, `doing>blocked`; this frees a slot), a card ENTERS DONE
  (`card_done`), a card ENTERS BLOCKED (`card_blocked`), a card ENTERS READY (a new candidate), and a repo-lease release. Sources:
  router `Tee to Bounce Recorder` on every card move (after dedupe, demo drops excluded; the moved card is excluded from its own
  pull); ready-prefetch `Final Output` once a card is readied and persisted (Advance Guard no longer moves it to DOING).
  doing-orchestrate lease_busy returns the card to READY instead of TODO. No schedule, sweep or watchdog ever calls it.
- ⛔ NOT ARMED (measured 2026-09-30, A5). Arming needs ALL of: fr `AA_WIP_LIMITS` set (fr#1277; today unset); n8n
  `AA_READY_PULL_ENABLED=true` (unset cluster-wide); `exec_v2_rollout.json` `webhook_entries["e.wip-pull"]: true` (today `{}`);
  `AA Entry - wip-pull` active (today active=false); n8n-nodes-fr#26 published + baked; fr declaring `wip_slot_id` in its Runtime
  State field group (without it every board is skipped `wip_slot_field_missing`: nothing is ever claimed); fr and n8n's mirror both
  carrying the same project-only value `{"project":{"default":1}}` (George 2026-09-30; replaces "equal to the Q2 values"); and
  George's explicit go (arming makes the conveyor run; it is not part of this design change). Arming
  blockers are owned by ncn#1913/#1914. Proof: `scripts/tests/test-wip-pull.mjs` over `tests/scenarios/steps/s.wip.pull/` +
  `tests/scenarios/entries/e.wip-pull/`.

Flow Type Catalogue
Classification flows - project, card, card_comment, frontend_user_input, frontend_approval_comments, frontend_user_question
Card manipulation flows - create, update, move, comment
Project management flows - create, update, report, analyze
Collection flows (inc approval)) - information gathering/collection from sources 
Processing flows - processing collected information into packages
Generation flows - generating content either text or a structured format according to an output schema
Evaluation flows - static inspection of information resulting in text (commentary) and/or a structured output (e.g. boolean)
Validation flows - test execution, goal-outcome-valuation
(Addition: Classification and Evaluation flows are offer→select — an LLM never returns a free label, route or verdict; TI §12, §12a.)


Worker Behavior Catalogue:
George's text:
> project_worker_behaviors:
> generate project_start_request - following project_start_request_template
> classify project_type - classification of project_type based on project_start_request
> generate card_generation_request - following card_generation_request_template
> classify card_type - based on available card_types and card_generation_request
> create card based on card_type and card_generation_request
> update card based on comment (block/unblock message), card_type and card_generation_request
>
> content_worker_behaviors:
> decide keywords - for example for principles / requirements / decisions search
> select most applicable item - for example principles / requirements / decisions based on search results
> validate applicability of item - for example selected principles / requirements / decisions
> validate sufficiency of item - for example principles / requirements / decisions
> request creation of item - for example principles / requirements / decisions

Implementation (TI §12, §12a):
Rule: a worker is single-turn and tool-less. A behaviour that CHOOSES is offer→select: one offer call returns schema-validated
options, one select call returns a single word from a named enum, checked by `a.compute.options.validateChoice` (agreement mode
per `r.select.agreed`, owned by movies-70). A behaviour that GENERATES writes a schema-validated document into a declared field and
chooses nothing. Anything that can be computed is computed. Anything that cannot be computed or offer→selected goes to BLOCKED
as a measured event (`no_computed_check:<card_type>`), never to a judge. No behaviour here picks a list, a route or a verdict freely.
- generate project_start_request: generation into the project_start_request_template schema; no choice.
- classify project_type: input = the project_start_request (as George wrote). Select ONE word from the enum of the project type
  catalogue above {program, meta_prompt, meta_procedure, kaizen, it}. The project's tenant then follows from the type (fr
  `PROJECT_TYPE_TENANT`), never from the model.
- generate card_generation_request: generation into the card_generation_request_template schema; no choice.
- classify card_type: inputs = the available card_types and the card_generation_request (as George wrote). Offer ≤4 candidate card
  types, then select ONE word from the offered set. The enum is the card-type vocabulary at the card's recorded version (source:
  unsettled vocabulary question, owned by the coordinator).
- create card: computed (kanban card create from card_template + the card_generation_request); no model call.
- update card from a comment: a `!block` / `!unblock` / `!escalate` command is parsed by the router, no model. A free-text comment:
  offer ≤4 field updates, then select ONE word from {offered update ids} ∪ {no_change}.
- decide keywords: generation of search terms (`infer/search_terms.json`); the search result set is computed, not chosen.
- select most applicable item: shortlist (`infer/shortlist.json`), ids ∈ the candidate ids the search returned; then
  `infer/select_option.json` from that set.
- validate applicability of item: `infer/validate_applicability.json`, enum {yes, partial, no}.
- validate sufficiency of item: `infer/validate_sufficiency.json` (`sufficient` + missing-kind enum {definition, callers,
  file_excerpt, requirement, decision, principle}); insufficient ⇒ BLOCKED `context_insufficient`.
- request creation of item: the default stays George's — the card goes to BLOCKED with a request to a HUMAN "to start project to
  create certain assets" (Board list catalogue, BLOCKED). Auto-minting a `*_development` prerequisite card whose `card_done`
  releases the waiting card (TI §9.5) is not adopted by this revision; it is listed in TI §16 as an open design item for the
  coordinator.

Card custom fields (create mapping logic)
parentCard / childCards
requires / isRequiredBy

Templates:
project_start_request_template
project_template
card_generation_request_template
card_template


Trigger Type Catalogue
George's text:
> WEBHOOKS: '[{"url": "http://workflow-api:8000/api/webhooks/planka", "accessToken": "${WEBHOOK_ACCESS_TOKEN}", "events": ["cardCreate","cardUpdate","cardMove","cardDelete","cardMembershipCreate","cardMembershipDelete","cardLabelCreate","cardLabelDelete","commentCreate","commentUpdate","commentDelete","attachmentCreate","attachmentDelete","taskCreate","taskUpdate","taskDelete","boardMembershipCreate","boardMembershipUpdate","boardMembershipDelete","listCreate","listUpdate","listDelete"], "excludedEvents": ["notificationCreate","notificationUpdate","notificationDelete"]}]'

Implementation (measured 2026-09-30, the one kanban webhook row):
- URL: `http://n8n.example.com:5678/webhook/aa-router` (A6). The `workflow-api:8000` URL above does not exist and must
  never be copied forward. The router's webhook path is `aa-router` (workflows/router.json:14); it checks `Authorization: Bearer`
  against `$env.AA_ROUTER_WEBHOOK_TOKEN`.
- Events on the live row: cardCreate, cardUpdate, cardDelete, cardMembershipCreate, cardMembershipDelete, cardLabelCreate,
  cardLabelDelete, commentCreate, commentUpdate, commentDelete, attachmentCreate, attachmentDelete, taskCreate, taskUpdate,
  taskDelete, listCreate, listUpdate, listDelete (18). The live row has no `cardMove` and no `boardMembership*` event, and
  `excluded_events` is NULL.

Trigger table (added 2026-09-30). Classes: event · per-card deadline (event-armed timer: armed by one card's own event, acts on
that card only; the one sanctioned timer, a flat 10-minute system-fault safety net, George 2026-09-30 Q1) · clock-observability (a timer that only measures or reports and
never moves a card or starts work; KEEP) · clock-mover (a timer that changes conveyor state; REMOVE or CONVERT). Clock rows: live
state measured 2026-09-30 (A1).

| Trigger | Kind | Class | Replacement event / note | Owner |
|---|---|---|---|---|
| Planka card event → `AA Router` (workflows/router.json, `aa-router`) | webhook | event | — (the source of every board move) | router |
| Router → list cells (workflows/cells/{todo,ready,doing,qa,blocked,done}.json) | webhook | event | — | router |
| Pattern completion / failure envelope | sub-workflow return | event | DOING→QA on ok:true, else DOING→BLOCKED | doing-orchestrate |
| `stopwatch_expired` (per-card deadline) | per-card one-shot | per-card deadline (event-armed timer) | armed by the card's DOING-entry event, cancelled by its DOING exit; flat 10 min; expiry → BLOCKED `stopwatch_expired: system fault`, no split (Q1); NOT BUILT (fr stopwatch-tick and the watchdog census are deleted) | movies-70 |
| WIP pull `AA Entry - wip-pull` (`aa-wip-pull`) | webhook | event | DOING-exit / DONE / BLOCKED / READY-entry / lease release; NOT ARMED | ncn#1913/#1914 (arming blockers) |
| fr approval decided → approval-callback (workflows/patterns/approval-callback.json) | webhook | event | releases BLOCKED→READY; its `Merge PR` node (:581) is removed — the DONE chain merges (TI §9.4) | DONE-chain lane |
| `card_done` from the DONE chain | event | event | releases dependents: TODO→READY evaluation + WIP pull | DONE-chain lane |
| kanban-bounce-recorder (router Tee) → `kanban_bounce_log` | sub-workflow | event (L2 sensor, S1/S2) | RETAINED as the L2 transition store (the only writer that sees every move) until exec_step stamping (L3) is live; target: also emits the S1 `outcome_event` (TI §11.1, §11.3) | C2-03/C2-28 |
| system/error-handler | errorTrigger | event (sensor S8) | — | observability |
| grafana-alert-receiver | webhook | event (alert intake) | pushes no card work | observability |
| AA Maintenance - Remint Expired Legacy Approval | webhook | event (REMOVE) | no spec asks for re-mint; approvals are released only by §9.5 events. Active, 0 runs (A1) | ncn#1875 |
| detector-close-card-on-done | 1 min | clock-mover (REMOVE) | slot closed by the DONE chain (TI §9.4). Active, 1,431 runs/24h (A1) | ncn#1875 |
| canary-workflow-rollback-watch | 5 min | clock-mover (CONVERT) | promote/discard on each canary execution-end event (`r.canary-verdict`, TI §11.4); CANARY runs only on execution `…0003` cards, customers never receive CANARY. Active, 286/24h | C2-15 + ncn#1875 |
| AA Maintenance - Canary Sweep | 6 h | clock-mover (REMOVE) | a step of the promote/discard event. Active, 4/24h | ncn#1875 |
| AA Observability - GitHub Outcome Poll | 30 min | clock-mover (REMOVE) | landing outcome emitted inside the DONE chain. Active, 48/24h | ncn#1875; emitters C2-03/C2-28 |
| AA - Eval-Set Defect Seed | daily 04:00 | clock-mover (REMOVE) | none: a clock KAG write of conveyor data (kag = user data only). Inactive | ncn#1875 |
| AA - Conveyor Stats | 5 min | clock-observability (KEEP) | pushes no work. Active, 288/24h | observability |
| AA Maintenance - Router Health Check | 5 min | clock-observability (KEEP) | synthetic probe. Active, 286/24h | observability |
| AA Observability - Kaizen Mint Census | 15 min | clock-observability (KEEP) | pushes no work. Active, 96/24h | observability |
| AA Pattern Compliance Checker | 6 h | clock-observability (KEEP) | inactive; no live consumer of its artifact (A6) — remove if none appears | observability |
| watchdog | 2 min | clock-observability (KEEP, quiesced) | sensor only (posts `watchdog-event`); never moves a card; its stopwatch role → the per-card 10-minute safety net. Inactive since 09-27 | observability |


classSize = file.class.size
for class in file
if classSize=0
	for function in class
else
	for function in file
	context: [code_analysis_outputs, linter_outputs, project/app/module_brief]
	procedure: root-cause-detection, code-evaluation, code-rewrite
