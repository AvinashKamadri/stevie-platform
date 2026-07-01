-- Migration 015 — human-review audit log (Phase 3).
--
-- Every review action (merge/distinct/related), independent of whether it
-- also produced a durable organization_merge_decision row. Two reasons this
-- exists as its OWN table rather than just reading organization_merge_decision:
--
-- 1. organization_merge_decision only supports decision IN ('merge','distinct')
--    — there is no 'related' value, and adding one would mean 'related' starts
--    participating in canonicalize's replay logic, which is explicitly NOT
--    ready (the relationship graph — parent/subsidiary/foundation structure —
--    is a separate, later project, not part of this workflow). A 'related'
--    verdict is recorded here ONLY, as durable seed data for that future work,
--    without pretending it already has a home in replay.
-- 2. organization_merge_decision's `unique (loser_key)` means a key can be
--    settled (merged away or marked distinct) only ONCE, globally — by
--    design (see M0_DECISION_STORE_DESIGN.md). This log has no such
--    constraint, so it is also the full audit trail: which SPECIFIC pairs a
--    reviewer looked at, even ones a stricter table couldn't represent
--    (e.g. a second 'distinct' verdict against an already-settled key).
--
-- This table never feeds canonicalize. It is a review-queue exclusion source
-- (don't re-show a pair already looked at) and an audit/seed record — nothing
-- more.

create table if not exists organization_review_log (
    id            bigserial primary key,
    left_key      text not null,
    right_key     text not null,
    action        text not null check (action in ('merge', 'distinct', 'related')),
    lane          text not null check (lane in ('main', 'acronym')),
    model_version text,             -- which model's score informed this action, if any
    probability   numeric,          -- calibrated probability at review time, if any
    reviewed_by   text not null,
    reviewed_at   timestamptz not null default now(),
    notes         text,
    check (left_key < right_key)
);
create index if not exists org_review_log_pair_idx on organization_review_log (left_key, right_key);

comment on table organization_review_log is
    'Append-only audit trail of every human review action (merge/distinct/'
    'related), independent of organization_merge_decision. The durable home '
    'for related verdicts (no replay-relevant table exists for them yet) and '
    'the review-queue exclusion source (do not re-show an already-reviewed pair).';
