# Job Applier

Full build spec: [docs/SPEC.md](docs/SPEC.md). It is the single source of truth. Read the relevant section before working on a feature.

## Working rules

- Build in the phases from SPEC §15, one at a time: plan → show plan + file list → implement → test → summarize → **stop and wait for approval**.
- Never hardcode model names, providers, URLs, limits, thresholds, schedules or prices. Everything comes from config (`config/*.yaml`, validated by `config/schema.json`).
- Never put secrets in code, config or logs. Config refers to secrets as `secret:<name>`; local dev resolves them from env vars.
- Ask before adding any paid third-party service or any AWS service not in the spec.
- Every stage emits structured logs, metrics, an `events` row (via `emit_event()`), and propagates the trace ID (`traceparent`).
- Ship tests with every feature and keep `make test` green.
- Prefer boring, well-known libraries. Keep each service small.

## Hard rules (product guardrails)

- **Job sources.** Only the official APIs and public feeds in SPEC §6.1. Never scrape LinkedIn, Indeed, Naukri or Glassdoor. Respect robots.txt on company career pages.
- **Sending channel.** Outreach goes only from the candidate's own mailbox (Gmail API or Microsoft Graph via OAuth). Never send outreach through Amazon SES; SES is only for our own transactional emails to our users.
- **No invented facts.** An email may only state facts in the candidate's confirmed resume. Every draft must pass `check_claims` before it can be approved.
- **Approval.** Default mode is "review each email". Auto-send only when the user turns it on and global config allows it.
- **Sending limits** (all from config). The sender enforces: per-user daily caps, random gaps between sends, recipient business hours, one email per company per role, contact cooldowns, at most one follow-up, a global suppression list.
- **Opt-out.** Every email includes the one-line opt-out from config. Opt-outs go to the suppression list.
- **Verification.** Verify every recipient before sending. By default skip catch-all and unknown results.
- **PII.** Never log resume text, email bodies, OAuth tokens or full email addresses. Encrypt PII at rest with KMS. Users can export and delete their data.

## Repo layout

```
├── CLAUDE.md
├── docs/SPEC.md
├── apps/web/                 # React SPA: candidate app + /admin
├── services/api/             # FastAPI
├── services/workers/         # fetch, normalize, match, contact, draft, send
├── services/model_gateway/
├── packages/common/          # config, db models, logging, tracing, events, metrics, queues, pricing
├── packages/adapters/        # job sources, contact finders, verifiers, mail senders
├── prompts/                  # versioned prompt templates (synced to S3)
├── config/                   # local.yaml, schema.json
├── infra/                    # AWS CDK (TypeScript)
├── docker-compose.yml
└── Makefile
```

## Commands

| Command | What it does |
|---|---|
| `make venv` | Create `.venv` (Python 3.12, pip) with all packages editable |
| `make up` | Start the local stack (docker compose) and apply migrations |
| `make down` | Stop the local stack |
| `make test` | Run all tests (DB tests need `make up`) |
| `make test-unit` | Tests that need no running services |
| `make lint` | ruff + ruff format --check + mypy |
| `make fmt` | Auto-format and fix lint |
| `make smoke` | Check that an event lands in `events` and its trace in Jaeger (localhost:16686) |
| `make migrate` | Apply Alembic migrations |
| `make seed` | Load local seed data |
| `make e2e` | End-to-end test in `dry_run` |
| `make deploy` | `cdk deploy` (Phase 10+) |
