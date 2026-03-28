# External Registration Failure Category Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a stable `failure_category` field to external registration batch responses and persist it at batch level for SQLite-backed deployments.

**Architecture:** Persist one new batch-level column, compute it centrally when a batch reaches terminal state, and expose it through the shared serializer so create/get/cancel/idempotent replay all stay consistent. Keep the implementation minimal: no item-level schema changes, no route-layer inference, no PostgreSQL migration work.

**Tech Stack:** Python, FastAPI, Pydantic, SQLAlchemy, SQLite, pytest

---

## Chunk 1: Workspace and baseline

### Task 1: Safe isolated workspace

**Files:**
- Create: `docs/superpowers/plans/2026-03-27-external-failure-category.md`

- [x] **Step 1: Create a dedicated worktree and branch**
- [x] **Step 2: Run baseline tests in the worktree**
- [x] **Step 3: Record the plan in this file before code changes**

## Chunk 2: Tests first

### Task 2: Route and documentation-facing tests

**Files:**
- Modify: `tests/test_external_api_routes.py`
- Modify: `docs/external-api-quickstart.md`
- Modify: `docs/external-api-examples.md`

- [x] **Step 1: Write failing route tests for `failure_category` presence on create/get/cancel/idempotent replay**
- [x] **Step 2: Run the route test subset and confirm the new assertions fail for the expected reason**
- [x] **Step 3: Update external API docs after implementation is stable**

### Task 3: Service and migration tests

**Files:**
- Modify: `tests/test_external_batch_service.py`

- [x] **Step 1: Write failing tests for summary classification, recovery classification, unknown fallback, and non-failed null behavior**
- [x] **Step 2: Include a SQLite migration coverage test for adding the new column to an existing DB**
- [x] **Step 3: Run the service test subset and confirm the new assertions fail for the expected reason**

## Chunk 3: Minimal production changes

### Task 4: Persist and compute `failure_category`

**Files:**
- Modify: `src/database/models.py`
- Modify: `src/database/session.py`
- Modify: `src/core/external_batches/service.py`
- Modify: `src/core/external_batches/recovery.py`

- [x] **Step 1: Add `failure_category` to `ExternalRegistrationBatch`**
- [x] **Step 2: Add SQLite auto-migration support for the new column**
- [x] **Step 3: Add a single classification helper in the batch service**
- [x] **Step 4: Update summary recomputation to set category on `failed` and clear it otherwise**
- [x] **Step 5: Update recovery logic to persist `transient` for `service_restarted`**
- [x] **Step 6: Update the shared serializer to return `failure_category`**

## Chunk 4: Verification and review closure

### Task 5: Verification

**Files:**
- Verify: `tests/test_external_api_routes.py`
- Verify: `tests/test_external_batch_service.py`

- [x] **Step 1: Run focused tests until green**
- [x] **Step 2: Run the combined external test suite**
- [x] **Step 3: Inspect diffs and confirm only intended files changed**

### Task 6: Review loop

**Files:**
- Review: all changed files

- [x] **Step 1: Run spec-compliance review**
- [x] **Step 2: Fix any findings and re-run spec review until zero issues**
- [x] **Step 3: Run code-quality review**
- [x] **Step 4: Fix any findings and re-run quality review until zero issues**
- [x] **Step 5: Run final whole-change review and close only when zero issues remain**

## Locked behavior

- Batch response adds `failure_category: "config" | "business" | "transient" | null`
- `POST /api/external/registration/batches`, `GET /api/external/registration/batches/{batch_uuid}`, and `POST /api/external/registration/batches/{batch_uuid}/cancel` all return it
- Non-`failed` statuses always serialize `failure_category = null`
- `failed` batches must serialize one of `config | business | transient`
- Idempotent replay returns the originally persisted category
- Classification mapping:
  - `service_restarted` -> `transient`
  - upload/email service missing, disabled, wrong type/provider, unsupported provider, no enabled services -> `config`
  - `outlook requested_service_id cannot be reused when count > 1` -> `business`
  - `no_available_email_service` -> `transient`
  - `registration_failed` and unknown reasons -> `transient`
