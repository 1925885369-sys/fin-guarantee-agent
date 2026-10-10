# Profile Contract v1

## Boundary

- An entity profile is reusable issuer master data. New bond projects require only `issuer_profile_id`.
- Guarantors and counter-guarantors are project subjects. Legacy profile role bindings remain readable.
- A profile update creates an immutable version. A project uses an immutable snapshot until a user refreshes it.
- Profile evidence is stored separately from uploaded project documents and does not count toward upload limits or material inventory.
- Agent analysis reads the union exposed by `analysis_documents` and `analysis_sources`.

## Status

Fact status is one of:

```text
confirmed | single_source | stale | conflicted | insufficient
```

`confirmed` and an unconflicted `single_source` fact may be used in deterministic calculations. `stale`, `conflicted`, and `insufficient` facts remain visible but may not supply calculated values. A source-backed `single_source` fact is an information notice, not an open review task.

## Profile Detail

`GET /api/entity-profiles/{id}` returns the existing summary fields plus:

```text
facts[]
shareholders[]
financial_periods[]
debt_instruments[]
credit_facilities[]
concentrations[]
related_transactions[]
legal_events[]
material_packages[]
sources[]
```

All repeatable records carry `id`, `profile_id`, `version`, `period` or `as_of_date` where applicable, `scope`, `status`, `valid_until`, `source_ids`, `reviewer`, and `reviewed_at`. Financial amounts also carry `unit` and `currency`.

## Readiness

`GET /api/entity-profiles/{id}/readiness` returns:

```json
{
  "profile_id": "string",
  "ready_for_project": true,
  "ready_for_agent": false,
  "complete_due_diligence_ready": false,
  "completeness": 0.0,
  "module_scores": [],
  "hard_vetoes": [],
  "requirements": [],
  "notices": [],
  "counts": {}
}
```

Module weights are identity 10%, ownership 15%, financial 30%, debt and contingencies 20%, operations 10%, customer/supplier 5%, related parties 5%, and legal/compliance 5%.

The Agent is blocked when legal identity, control conclusion, three years plus latest-period core financials, current debt/maturity structure, or major legal screening is missing, stale, conflicted, or insufficient. Profile creation and project creation remain allowed.

## Version And Import APIs

```text
GET  /api/entity-profiles/{id}/versions
GET  /api/entity-profiles/{id}/versions/{version}
POST /api/entity-profile-imports
GET  /api/entity-profile-imports/{job_id}
POST /api/entity-profile-imports/{job_id}/commit
```

An import accepts an Excel or JSON structured file plus evidence files. Parsing creates candidates and issues. Commit accepts only explicitly selected or corrected candidates with valid source references, then creates a new profile or a new immutable version.

## Snapshot APIs

```text
GET  /api/projects/{id}/profile-snapshot
POST /api/projects/{id}/profile-snapshot/refresh
GET  /api/projects/{id}/profile-snapshot/status
GET  /api/projects/{id}/profile-evidence
```

Snapshot status returns:

```text
snapshot_version
current_profile_version
has_newer_version
refresh_required
analysis_stale
evidence_status
```

A newer profile version is informational until refresh. Refresh changes the snapshot, sets `analysis_stale=true`, invalidates extraction and all downstream nodes, and blocks Word export until the Agent reruns.

## Evidence And Conflict Rules

- Profile source IDs projected into a project are stable functions of snapshot ID and original source ID.
- Only the current project snapshot is exposed through analysis views; historical snapshot evidence remains stored.
- Facts without a reliable quote and locator are not report-citable assertions.
- Assertions conflict only when subject, fact key, period, and scope match but values differ.
- Differences in period or consolidated/standalone scope are context differences, not conflicts.
- No source has automatic precedence. Prospectus and rating report assertions never overwrite the profile.
- Human resolutions are tied to the hash of their supporting assertion set and become stale when that set changes.

## Frontend Mock Rule

Profile fixtures are used only when `VITE_USE_PROFILE_MOCKS=true`. Real API failures never fall back to fixtures. Mock mode is visibly labelled, and a production build must reject the mock flag.
