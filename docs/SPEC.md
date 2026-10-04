# Job Applier: Build Spec

This file is the single source of truth for building the Job Applier. It is written for Claude Code.

Candidates sign up, upload a resume, set job preferences and connect their own Gmail or Outlook mailbox. Every few hours the system:

1. pulls new jobs from official job APIs and feeds,
2. matches them to each candidate,
3. finds a verified HR or recruiter email,
4. drafts a short email grounded only in the candidate's resume.

After the candidate approves a draft, the system sends it from the candidate's own mailbox with the resume attached.

Admins get a console showing everything: logs, traces, pipeline health, LLM usage and cost, email outcomes, configuration and an audit trail.

---

## 0. Working rules for Claude Code

1. Save this file as `docs/SPEC.md`. Create a short `CLAUDE.md` at the repo root containing:
   - section 1 (hard rules),
   - the repo layout (section 13),
   - the `make` commands,
   - a one-line pointer to `docs/SPEC.md`.

   Do not copy the whole spec into CLAUDE.md.
2. Build in the phases from section 15, one at a time. For each phase:
   - plan first and show me the plan and file list,
   - implement it,
   - run tests,
   - summarize what was done,
   - stop and wait for my approval before the next phase.
3. Never hardcode model names, providers, URLs, limits, thresholds, schedules or prices. Everything comes from config (section 9).
4. Never put secrets in code, config or logs. Config refers to secrets as `secret:<name>` (AWS Secrets Manager). Local dev uses env vars.
5. Ask me before adding any paid third-party service or any AWS service not listed here.
6. Observability is not optional. Every stage must:
   - emit structured logs,
   - emit metrics,
   - write an `events` row,
   - propagate the trace ID (section 11).

   Build this in from Phase 1.
7. Ship tests with every feature and keep `make test` green.
8. Prefer boring, well-known libraries. Keep each service small.

## 1. Hard rules (product guardrails)

- **Job sources.** Get jobs only from the official APIs and public feeds in section 6.1.
  - Never scrape LinkedIn, Indeed, Naukri or Glassdoor.
  - Respect robots.txt on company career pages.
- **Sending channel.** Send outreach only from the candidate's own mailbox (Gmail API or Microsoft Graph via OAuth).
  - Never send outreach through Amazon SES. SES is only for our own transactional emails to our users.
- **No invented facts.** An email may only state facts that are in the candidate's confirmed resume.
  - Every draft must pass a claim check (`check_claims`) before it can be approved.
- **Approval.** The default approval mode is "review each email". Auto-send is allowed only when the user turns it on and global config allows it.
- **Sending limits.** All values come from config. The sender must enforce:
  - per-user daily caps,
  - random gaps between sends,
  - the recipient's business hours,
  - one email per company per role,
  - contact cooldowns,
  - at most one follow-up,
  - a global suppression list.
- **Opt-out.** Every email includes a one-line opt-out (text from config). Opt-outs go to the suppression list.
- **Verification.** Verify every recipient address before sending. By default, skip catch-all and unknown results.
- **PII.**
  - Never log resume text, email bodies, OAuth tokens or full email addresses.
  - Encrypt PII at rest with KMS.
  - Users can export and delete their data.

## 2. Scope

**v1 includes:**
- candidate web app,
- ingestion,
- matching,
- HR contact discovery,
- drafting with claim check,
- approval,
- sending,
- admin console,
- full observability,
- AWS infrastructure as code.

**v1 non-goals:**
- auto-filling ATS application forms,
- LinkedIn integration,
- automatic reply and bounce detection (feature flag `reply_tracking`, off by default, because it needs read scopes),
- resume rewriting (feature flag `resume_tailoring`, off by default).

## 3. Tech stack

| Area | Choice |
|---|---|
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic |
| Workers | Python 3.12. One image with one entrypoint per stage, consuming SQS |
| Frontend | React + TypeScript + Vite, TanStack Query, Tailwind + shadcn/ui, Recharts. Candidate app and admin console in one SPA (admin under `/admin`) |
| Auth | Amazon Cognito (email + Google sign-in). Groups: `admin` (full) and `support` (read-only, PII masked) |
| Database | Amazon RDS for PostgreSQL 16 with pgvector |
| Queues | Amazon SQS, one queue + DLQ per stage |
| Scheduling | EventBridge Scheduler |
| Config | AWS AppConfig: one YAML profile per environment, read via the AppConfig Agent sidecar, validated by JSON Schema |
| Secrets | AWS Secrets Manager for app secrets. Per-user OAuth refresh tokens are KMS envelope-encrypted in Postgres |
| LLM | gpt-oss-20b on Amazon Bedrock via its OpenAI-compatible Chat Completions endpoint (default). Optional self-hosted llama.cpp server on Graviton. Both sit behind our model gateway |
| Embeddings / rerank | `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3` on Hugging Face Text Embeddings Inference (TEI), CPU |
| Compute | ECS Fargate, ARM64 where the image supports it (use x86_64 for TEI if its CPU image isn't available for ARM64) |
| Edge | CloudFront in front of both the SPA (S3 origin) and the API (`/api/*` → API Gateway HTTP API with Cognito JWT authorizer → VPC link → internal ALB → api service). AWS WAF on CloudFront |
| Observability | Structured JSON logs to CloudWatch Logs. CloudWatch metrics via Embedded Metric Format. OpenTelemetry traces to AWS X-Ray through the ADOT collector sidecar. CloudWatch alarms → SNS |
| IaC | AWS CDK (TypeScript) |
| CI/CD | GitHub Actions: lint, test, build images, `cdk deploy` to dev → staging → prod |
| Local dev | docker compose: Postgres + pgvector, ElasticMQ (SQS), MinIO (S3), TEI embed + rerank, Jaeger (traces), mock LLM or real Bedrock. Config from `config/local.yaml` |

## 4. Architecture

### Services

| Service | Runs on | Purpose |
|---|---|---|
| `web` | S3 + CloudFront | Candidate app and admin console |
| `api` | Fargate | REST API for the SPA, enqueues work, admin endpoints |
| `worker-fetch` | Fargate | Pulls jobs from sources |
| `worker-normalize` | Fargate | Rule-based field extraction and embeddings |
| `worker-match` | Fargate | Filters, vector search, rerank, LLM parse and fit score |
| `worker-contact` | Fargate | HR contact waterfall and verification |
| `worker-draft` | Fargate | LLM draft and claim check |
| `worker-send` | Fargate | Guardrails and sending via Gmail or Graph |
| `model-gateway` | Fargate (internal) | Single entry point for chat, embeddings and rerank. Routes from config and logs every call |
| `embedder`, `reranker` | Fargate (internal) | TEI on CPU |
| `llm-selfhosted` (optional, off) | EC2 Graviton (m8g.4xlarge) | llama.cpp server with gpt-oss-20b (GGUF) |

### Flow

```
EventBridge Scheduler ──► q.fetch ──► worker-fetch ──► jobs ──► q.normalize ──► worker-normalize
      (rules + embedding) ──► q.match ──► worker-match ──► matches ──► q.contact ──► worker-contact
      ──► contacts ──► q.draft ──► worker-draft ──► application(awaiting_approval)
      user approves in web ──► api ──► q.send ──► worker-send ──► Gmail API / Graph ──► HR inbox

Every model call: worker ──► model-gateway ──► Bedrock | llama.cpp | TEI
```

### Queue rules

- **Message bodies:** IDs only, never PII. Format: `{message_id, type, created_at, payload: {...ids}}`. Pass the trace context as the SQS message attribute `traceparent`.
- **Idempotency:** every consumer is idempotent (check entity state, dedupe by `message_id`).
- **Failures:** max 5 receives, then the message goes to the DLQ.
- **Scaling:** worker services scale on queue depth and can scale to zero.

## 5. Data model (PostgreSQL)

Use UUID primary keys and `created_at` / `updated_at` on every table.

- `users`: cognito_sub, email, name, role, status (active | paused | deleted), timezone
- `profiles`: user_id, phone, city, country, linkedin_url, portfolio_url, summary, total_years, current_title, current_company, notice_period, work_authorization, sponsorship_needed, pitch, signature, tone, profile_embedding vector(1024)
- `resumes`: user_id, s3_key, file_name, parsed_json, confirmed_json, confirmed_at, is_active
- `preferences`: user_id, target_titles[], seniority, must_have_keywords[], excluded_keywords[], job_types[], work_modes[], locations[], open_to_relocate, min_salary, currency, blocked_companies[], blocked_industries[], max_job_age_days, daily_cap, approval_mode, auto_send_min_score, follow_up_enabled
- `mailbox_connections`: user_id, provider (gmail | outlook), email, scopes[], refresh_token_ciphertext, kms_key_id, status (connected | expired | revoked), last_checked_at
- `companies`: name, domain, ats, ats_slug, careers_url
- `jobs`: source, external_id, company_id, title, location, work_mode, employment_type, salary_min, salary_max, currency, description, apply_url, posted_at, fetched_at, dedupe_hash (unique), emails_in_post[], embedding vector(1024), parsed_json, parsed_at
- `matches`: user_id, job_id (unique together), vector_score, rerank_score, fit_score, matched_skills[], missing_skills[], reason, status (new | shortlisted | drafted | skipped | dismissed)
- `contacts`: company_id, email, name, title, source (job_post | careers_page | hunter | apollo | name_pattern | generic_inbox), source_url, confidence, verification_status (valid | invalid | catch_all | unknown), verified_at
- `applications`: user_id, job_id, match_id, contact_id, status (drafting | needs_edit | awaiting_approval | approved | scheduled | sent | failed | no_contact | rejected_by_user | replied | bounced | follow_up_sent), subject, body, claim_check_json, approved_at, scheduled_for, sent_at, provider_message_id, provider_thread_id, follow_up_due_at, error
- `suppression_list`: email, domain, reason (opt_out | bounce | complaint | manual), source, created_by
- `llm_calls`: trace_id, task, provider, model, prompt_version, input_tokens, output_tokens, latency_ms, cost_usd, status, error_code, payload_s3_key, user_id, job_id, application_id
- `events` (append-only timeline): trace_id, entity_type, entity_id, user_id, stage, event_type, status (ok | error | skipped), duration_ms, details jsonb
- `pipeline_runs`: trace_id, source, started_at, finished_at, fetched, new_jobs, errors, status
- `external_api_usage`: provider, endpoint, units, cost_usd, status
- `audit_log`: actor_user_id, action, target_type, target_id, before jsonb, after jsonb, reason, ip
- `data_requests`: user_id, type (export | delete), status, completed_at

Indexes:
- HNSW on `jobs.embedding` (cosine)
- `events(trace_id)`
- `events(entity_type, entity_id)`
- `llm_calls(created_at)`
- `applications(user_id, status)`

Partition or prune `events` and `llm_calls` by month, using retention from config.

## 6. Pipeline stages

### 6.1 Fetch (`worker-fetch`)

**Adapter interface:** `name`, `fetch(since) -> Iterable[RawJob]`, plus per-adapter rate limits and retries.

**v1 adapters:**
- **Greenhouse:** `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- **Lever:** `GET https://api.lever.co/v0/postings/{slug}?mode=json`
- **Ashby:** `GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`
- **Arbeitnow:** `GET https://www.arbeitnow.com/api/job-board-api` (no key)
- **HN "Who is hiring?":** use the HN Algolia API to find the latest monthly story posted by the `whoishiring` account, then read its comments.

**Behind config flags:** Adzuna (app_id + app_key), Jooble, JSearch (RapidAPI).

**Company registry:** a CSV in S3 (company, domain, ats, slug). Admins can upload or edit it from the console.

**Processing:**
1. Normalize to the Job schema.
2. Compute `dedupe_hash = sha256(normalized company + title + location)`.
3. Upsert the job.
4. Enqueue new jobs to `q.normalize`.

**Errors:** write one `pipeline_runs` row per source per run. A failing source never fails the whole run.

### 6.2 Normalize + embed (`worker-normalize`)

- Strip HTML.
- Extract with rules: emails in the post, salary, work mode, employment type, posted date.
- Embed `title + company + requirements excerpt` via the gateway `/v1/embeddings` and store it in `jobs.embedding`.
- If `matching.llm_parse_only_shortlisted` is false, run `parse_job` here as well.
- Enqueue to `q.match`.

### 6.3 Match + score (`worker-match`)

1. **Find candidates.** For each new job, find active users who pass the hard filters in SQL: location or work mode, job type, blocked companies and keywords, posting age, sponsorship. Then rank them by cosine similarity with `profiles.profile_embedding`. A nightly per-user pass over recent jobs catches anything missed.
2. **Shortlist.** Keep `vector_top_k` per user.
3. **Rerank.** Call the gateway `/v1/rerank` and keep `rerank_top_k`.
4. **Parse the job.** Run `parse_job` (cached per job, run once for all users), then score fit with `score_fit`.
5. **Next step.** If `fit_score >= min_fit_score`, set status to `shortlisted` and enqueue to `q.contact`.

### 6.4 Contact discovery (`worker-contact`)

1. Resolve the company domain from the job data, ATS metadata, or a Hunter company lookup.
2. Run the waterfall in `contact_finder.order`, skipping disabled providers:
   - `job_post`: emails found in the posting
   - `careers_page`: the company's careers and contact pages, respecting robots.txt
   - `hunter`: domain search with `department=hr`, or the email finder for a named recruiter
   - `apollo`: optional
   - `name_pattern`: recruiter name + the company's pattern from Hunter
   - `generic_inbox`: only if allowed
3. Verify each candidate address with the configured verifier.
4. Stop at the first contact with `confidence >= min_confidence` and `verification_status = valid`.
5. Cache contacts per company for `cache_days`, reusing them across users.
6. If nothing qualifies, set the application to `no_contact` and show the candidate the official apply link.
7. Record every paid API call in `external_api_usage`.

### 6.5 Draft (`worker-draft`)

1. Run `draft_email` with the parsed job, the user's confirmed resume facts, their pitch, tone and signature.
2. Run `check_claims`. If any claim is unsupported, regenerate (max 2 tries), otherwise set status `needs_edit`.
3. Set status `awaiting_approval` and notify the user via SES (digest or reminder, per config).

### 6.6 Approve (web)

The user sees:
- the job,
- the draft,
- the contact (with source and confidence),
- the claim-check result.

Actions: edit, approve, reject, or opt in to auto-send above a score (only if `email.allow_auto_send` is true).

### 6.7 Send (`worker-send`)

**Before sending, check all of these:**
- mailbox connected,
- user not paused,
- suppression list (email and domain),
- daily cap,
- contact cooldown,
- one email per company per role,
- recipient business hours (timezone from the job location, else `default_recipient_timezone`),
- `email.dry_run` (if true, log and record instead of sending).

**Sending:**
1. Schedule with a random gap.
2. Build a MIME email with the resume PDF attached (`attachment_name` from config).
3. Send via the Gmail API `users.messages.send` (raw base64url MIME) or Microsoft Graph `POST /me/sendMail`.
4. Store the provider IDs and set status `sent`.
5. Schedule a follow-up if enabled.

**Errors:**
- auth expired → mark the mailbox `expired` and notify the user,
- throttled → back off,
- permanent → `failed` with a reason.

**Replies and bounces:** in v1 the user marks replied or bounced in the app. Bounced addresses go to the suppression list.

### 6.8 LLM tasks and output contracts

Every task returns JSON, validated with Pydantic. On invalid output, retry once, then fail the task with an `events` row.

- `parse_resume` → `{name, email, phone, location, summary, skills[], experience[{title, company, start, end, achievements[]}], education[], certifications[], projects[], links{}}`
- `parse_job` → `{title, seniority, employment_type, work_mode, locations[], salary{min, max, currency, period}, years_experience_min, must_have_skills[], nice_to_have_skills[], visa_sponsorship (yes | no | unknown), recruiter_name, contact_emails[], summary}`
- `score_fit` → `{score (0-100), matched_skills[], missing_skills[], dealbreakers[], reason (max 40 words)}`
- `draft_email` → `{subject, body, claims_used[]}`. The body is 120–150 words and contains:
  - the exact role and job ID,
  - two achievements that map to the job's requirements,
  - the official apply link,
  - the opt-out line,
  - the signature.
- `check_claims` → `{ok: bool, claims: [{text, supported: bool, evidence}]}`

Prompts live in `prompts/<task>/<version>.md`, synced to S3. The version used is recorded on every `llm_calls` row.

## 7. Model gateway (`model-gateway`)

Build this as our own small internal FastAPI service, so that logging and cost tracking live in our code. Do not use LiteLLM unless I approve it.

### Endpoints

- **`POST /v1/chat/completions` (OpenAI-compatible).** Callers set `model` to a task name, for example `draft_email`. The gateway maps the task to a provider, model and parameters from `llm.tasks`.
- **`POST /v1/embeddings`.** Proxies to the embedder.
- **`POST /v1/rerank`.** Proxies to the reranker.

### Providers

Both providers are OpenAI-compatible endpoints from config:
- **Bedrock:** `https://bedrock-runtime.<region>.amazonaws.com/openai/v1`, model `openai.gpt-oss-20b-1:0`.
- **Self-hosted:** llama.cpp server at `/v1`.

For Bedrock auth, use the service's IAM role. If the endpoint needs a Bedrock API key, generate short-term keys from the role and refresh them before expiry. Never use long-term keys in production.

### Behaviour

- Passes `reasoning_effort`, `temperature` and `max_tokens` per task.
- Retries with exponential backoff on throttling.
- Uses the fallback provider if it is enabled.
- Has a circuit breaker and per-provider concurrency limits.

### Logging every call

- one `llm_calls` row (tokens, latency, cost from `pricing`, status),
- EMF metrics,
- an OpenTelemetry span,
- optionally the prompt and response to encrypted S3 (`observability.llm_payload_logging`), with retention from config.

The gateway hot-reloads config, so switching `default_provider` takes effect without a restart.

## 8. Candidate web app

### Pages

- **Sign up / sign in**
- **Onboarding wizard:**
  1. resume upload,
  2. parsed preview to confirm or edit,
  3. preferences,
  4. connect Gmail or Outlook,
  5. consent.
- **Dashboard:** today's matches with score and reasons, drafts awaiting approval, scheduled and sent emails, simple stats.
- **Job detail and draft editor**
- **Settings:** preferences, caps, approval mode, mailbox, signature, pitch, data export and delete.

### Fields to collect

| Group | Fields |
|---|---|
| Identity | Full name, email, phone, city and country, timezone, LinkedIn, GitHub/portfolio |
| Resume | PDF/DOCX upload. Parsed into skills, work history with achievements, education, certifications, projects, total experience, current title and company. The user confirms before use |
| Targets | 2–5 target titles, seniority, must-have and excluded keywords, job type, remote/hybrid/onsite, locations, open to relocate, expected salary and currency, notice period, work authorization / sponsorship need |
| Exclusions | Blocked companies (pre-fill the current employer), industries to avoid, max posting age |
| Outreach | Connected Gmail/Outlook, signature, 2–3 line pitch, tone, daily send cap, approval mode, follow-ups on/off |
| Consent | Permission to email on their behalf, confirmation the resume is accurate, data-retention choice |

### Mailbox OAuth

- **Gmail:** scope `https://www.googleapis.com/auth/gmail.send` only.
- **Outlook:** Microsoft Graph delegated permission `Mail.Send`.
- Encrypt refresh tokens with KMS envelope encryption.
- Check token health daily and show the status to the user and to admins.

## 9. Configuration

- **Storage.** All runtime settings live in one AWS AppConfig YAML profile per environment. Services read it through the AppConfig Agent (local HTTP on the default port, 2772) and hot-reload on change. Local dev reads `config/local.yaml`.
- **Validation.** Write `config/schema.json` (JSON Schema) for the full structure below. Validate in code at load time and register it as the AppConfig validator.
- **Admin edits.** The admin console can view and edit the config:
  - create a new hosted configuration version,
  - deploy it with the strategy from `admin.config_deployment_strategy`,
  - watch the deployment,
  - roll back.

  Every change writes to `audit_log` with a before/after diff.

Start from this config:

```yaml
environment: dev                          # dev | staging | prod
version: 1

llm:
  providers:
    bedrock:
      enabled: true
      base_url: https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1   # a region where gpt-oss-20b is enabled
      model: openai.gpt-oss-20b-1:0
      auth: bedrock_short_term_key        # generated from the service's IAM role
      timeout_seconds: 60
      max_concurrency: 20
    selfhosted:
      enabled: false
      base_url: http://llm.models.internal:8080/v1      # llama.cpp server on Graviton
      model: gpt-oss-20b
      auth: secret:selfhosted_llm_key
      timeout_seconds: 180
      max_concurrency: 4
  default_provider: bedrock
  fallback_provider: selfhosted           # used on errors or throttling, only if enabled
  retries: { max_attempts: 3, base_delay_ms: 500 }
  tasks:                                  # provider "default" = default_provider
    parse_resume: { provider: default, reasoning_effort: low,    temperature: 0,   max_tokens: 1500, prompt: parse_resume/v1.md }
    parse_job:    { provider: default, reasoning_effort: low,    temperature: 0,   max_tokens: 800,  prompt: parse_job/v1.md }
    score_fit:    { provider: default, reasoning_effort: medium, temperature: 0,   max_tokens: 600,  prompt: score_fit/v1.md }
    draft_email:  { provider: default, reasoning_effort: medium, temperature: 0.4, max_tokens: 900,  prompt: draft_email/v1.md }
    check_claims: { provider: default, reasoning_effort: low,    temperature: 0,   max_tokens: 400,  prompt: check_claims/v1.md }

pricing:                                  # USD per 1M tokens, for cost tracking. Verify current rates.
  bedrock:    { input: 0.07, output: 0.30 }
  selfhosted: { input: 0.0,  output: 0.0 }

embeddings:
  base_url: http://embedder.models.internal/v1
  model: BAAI/bge-m3
  dimensions: 1024
  batch_size: 32

reranker:
  base_url: http://reranker.models.internal
  model: BAAI/bge-reranker-v2-m3

prompts:
  bucket: s3://jobapplier-prompts-dev

matching:
  vector_top_k: 100
  rerank_top_k: 20
  min_fit_score: 70
  max_job_age_days: 14
  llm_parse_only_shortlisted: true

job_sources:
  schedule: rate(3 hours)                 # EventBridge Scheduler expression
  dedupe_on: [company, title, location]
  registry_file: s3://jobapplier-config-dev/company_registry.csv
  sources:
    greenhouse:     { enabled: true }
    lever:          { enabled: true }
    ashby:          { enabled: true }
    arbeitnow:      { enabled: true }
    hn_whos_hiring: { enabled: true }
    adzuna:         { enabled: false, countries: [gb, us], app_id: secret:adzuna_app_id, app_key: secret:adzuna_app_key }
    jooble:         { enabled: false, api_key: secret:jooble_api_key }
    jsearch:        { enabled: false, api_key: secret:rapidapi_key }

contact_finder:
  order: [job_post, careers_page, hunter, apollo, name_pattern, generic_inbox]
  providers:
    hunter: { enabled: true,  api_key: secret:hunter_api_key, department: hr }
    apollo: { enabled: false, api_key: secret:apollo_api_key, titles: [Recruiter, Talent Acquisition, HR Manager] }
  verifier: { provider: zerobounce, api_key: secret:zerobounce_api_key }   # zerobounce | neverbounce | hunter
  min_confidence: 0.7
  skip_catch_all: true
  allow_generic_inbox: false
  cache_days: 30
  respect_robots_txt: true

email:
  dry_run: true                           # true in dev/staging: record instead of sending
  providers: [gmail, outlook]
  daily_cap_per_user: 20
  gap_minutes: { min: 3, max: 12 }
  send_window_recipient_local: "09:30-17:30"
  default_recipient_timezone: UTC
  approval_mode: review_each              # review_each | auto_above_score
  allow_auto_send: false                  # global switch for auto_above_score
  auto_send_min_score: 85
  one_email_per_company_role: true
  contact_cooldown_days: 30
  follow_up: { enabled: true, after_business_days: 5, max_count: 1 }
  attachment_name: "{first}_{last}_Resume.pdf"
  opt_out_line: "If you're not the right person, or would rather not hear from me, just let me know."

notifications:                            # SES, our own users only
  ses_from: notify@yourdomain.com
  daily_digest: true
  approval_reminders: true

observability:
  log_level: INFO
  log_retention_days: 30
  events_retention_days: 180
  llm_payload_logging: true               # prompts and responses to encrypted S3, admin-only
  llm_payload_retention_days: 14
  trace_sample_rate: 1.0
  metrics_namespace: JobApplier
  alerts:
    emails: [admin@yourdomain.com]
    dlq_messages_gt: 0
    queue_oldest_age_minutes_gt: 30
    stage_error_rate_pct_gt: 5
    llm_error_rate_pct_gt: 5
    llm_p95_latency_seconds_gt: 60
    daily_llm_cost_usd_gt: 20
    send_failure_rate_pct_gt: 5
    reported_bounce_rate_pct_gt: 3
    source_stale_hours_gt: 12
    api_5xx_rate_pct_gt: 2

admin:
  full_access_group: admin
  read_only_group: support
  mask_pii_by_default: true
  require_reason_to_reveal_pii: true
  allow_config_edit_in_ui: true
  config_deployment_strategy: AppConfig.Linear50PercentEvery30Seconds

features:
  reply_tracking: false                   # needs read scopes (restricted for Gmail)
  resume_tailoring: false
```

## 10. Admin console (`/admin`)

### Access

- Cognito group `admin` has full access. Group `support` is read-only with PII always masked.
- All routes live under `/admin/api/*` and are authorized server-side.
- Every admin action writes to `audit_log`.
- Emails are masked by default (`j***@acme.com`). Revealing one requires a reason, which is audited.
- No user impersonation.

### Pages

1. **Overview**
   - Health tiles per stage: queue depth, age of oldest message, DLQ count, error rate, p95 duration.
   - Funnel for 24h / 7d / 30d: fetched → new → matched → shortlisted → contact found → drafted → approved → sent → replied / bounced.
   - LLM tokens and cost (today and month to date).
   - Third-party API spend.
   - Active alarms.
2. **Trace explorer**
   - Search by trace ID, user, job, application or contact email.
   - Shows the full timeline from `events`, the linked `llm_calls`, matching log lines (CloudWatch Logs Insights filtered by trace_id) and a link to the X-Ray trace.
3. **Logs**
   - Logs Insights queries with filters: service, level, time range, free text, any ID.
   - Saved queries: errors in the last hour, by trace_id, by user_id, sender failures, gateway errors.
   - Results table with expandable JSON and CSV export.
4. **Pipeline**
   - Runs per source: fetched, new, errors, duration.
   - Throughput and failures per stage.
   - DLQ browser: view a message, redrive, delete (admin only, audited).
5. **LLM**
   - Calls table: task, provider, model, prompt version, tokens, latency, cost, status.
   - Charts by task and provider, plus an error breakdown.
   - Payload viewer (admin only, audited).
   - Usage per prompt version.
6. **Outreach**
   - Sends per day, cap hits, failures by reason, reported bounces and opt-outs.
   - Suppression list management (add or remove, with a reason).
   - Per-recipient-domain stats.
7. **Contacts**
   - Hit rate per waterfall source.
   - Verification outcomes.
   - Credits and cost per provider.
8. **Sources**
   - Health per source: last success, items per run, error rate.
   - Enable or disable a source (writes config through AppConfig).
   - Company registry upload and edit.
9. **Users**
   - Search, status, mailbox token health, usage, pause or resume sending.
   - Data export and delete requests.
10. **Config**
    - Current version, YAML view, edit with schema validation.
    - Diff against the current version, deploy, deployment status, rollback, history.
11. **Audit log**
    - All admin actions with before/after.
12. **Alerts**
    - Current CloudWatch alarm states, thresholds from config, notification targets.
13. **Costs**
    - LLM cost from `llm_calls`.
    - Third-party cost from `external_api_usage`.
    - Optional AWS cost by service via the Cost Explorer API (daily).

### Data sources

- Postgres: business data, `events`, `llm_calls`, `audit_log`.
- CloudWatch `GetMetricData` for charts.
- CloudWatch Logs Insights (`StartQuery` / `GetQueryResults`) for logs.
- X-Ray for trace links.
- SQS attributes for queue and DLQ state.
- AppConfig for config.

### Pluggable backends

Put logs, metrics and traces behind small interfaces (`LogStore`, `MetricStore`, `TraceLinker`) so local dev works:
- **Logs:** read JSON log files from a shared volume.
- **Metrics:** compute from Postgres `events`.
- **Traces:** Jaeger.

## 11. Observability (build from Phase 1)

### Logging

- JSON to stdout via structlog.
- Required fields: `timestamp, level, service, env, version, trace_id, span_id, user_id, job_id, application_id, stage, event, duration_ms, error_type, message`.
- IDs only. No PII (section 1).
- One log group per service. Retention from config.

### Tracing

- OpenTelemetry SDK in every service, with the ADOT collector sidecar exporting to X-Ray (Jaeger locally).
- Propagate `traceparent` through HTTP headers and SQS message attributes.
- Each job gets its own trace at fetch time, linked to its `pipeline_runs` row.

### Events

A shared `emit_event()` helper:
- writes the `events` row,
- logs,
- increments metrics.

Call it at the start, end and error of every stage, and on every status change.

### Metrics

Use namespace from config with dimensions `env` and `stage`, `source`, `task` or `provider` as relevant.

- **Pipeline:** `jobs_fetched, jobs_new, jobs_deduped, embeddings_created, matches_created, shortlisted, contacts_found, contacts_verified, drafts_created, claim_check_failed, approvals, emails_sent, emails_failed, bounces_reported, opt_outs, cap_hits`
- **LLM:** `llm_calls, llm_errors, llm_latency_ms, llm_input_tokens, llm_output_tokens, llm_cost_usd`
- **Stages and external APIs:** `stage_duration_ms, stage_errors, external_api_calls, external_api_errors`

### Alarms

Thresholds come from `observability.alerts`. Alarms go to SNS, then to email (optional Slack).

- any DLQ message,
- oldest queue message age,
- stage error rate,
- LLM error rate and p95 latency,
- daily LLM cost,
- send failure rate,
- reported bounce rate,
- a source with no successful fetch,
- API 5xx rate.

### Dashboard

Also create a CloudWatch dashboard in CDK that mirrors the admin Overview, for when the app itself is down.

## 12. Security

- IAM least privilege per service.
- KMS keys for PII and tokens.
- S3 buckets private with SSE-KMS.
- RDS encrypted, in private subnets.
- VPC endpoints for S3, SQS, Secrets Manager, AppConfig and Bedrock.
- WAF on CloudFront.
- CSP headers on the SPA.
- Admin routes double-checked server-side.
- Rate-limit the public API.
- Data export and delete fully implemented (delete removes S3 objects, revokes OAuth tokens and anonymizes rows).

## 13. Repo layout

```
job-applier/
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
└── Makefile                  # up, down, test, lint, migrate, seed, e2e, deploy
```

## 14. Testing

- **Unit tests** for every adapter, using recorded HTTP fixtures (no live calls in CI).
- **Contract tests** for the gateway against an OpenAI-compatible mock. Bedrock and llama.cpp must both work through the same code path.
- **Prompt eval set:**
  - at least 20 job descriptions with expected `parse_job` JSON,
  - at least 20 drafts with injected false claims that `check_claims` must catch.
- **End-to-end test** in `dry_run` with a fake mailbox provider: fetch → normalize → match → contact → draft → approve → "sent", asserting the events timeline.
- **Load test** in staging: 1,000 users and 10,000 new jobs per day.

## 15. Build phases

Each phase ends with tests passing and a short summary. Wait for approval after each phase.

1. **Foundation.** Repo, docker compose, Makefile, CI. Common package:
   - config loader with schema validation and hot reload,
   - structlog,
   - OpenTelemetry,
   - `emit_event`,
   - EMF metrics helper.

   Also the DB schema with migrations and health endpoints.

   *Done when* `make up && make test` pass, a sample event lands in `events`, and a trace shows in local Jaeger.
2. **Model gateway.** Routing from config, Bedrock and llama.cpp providers, TEI embed and rerank, retries, fallback, `llm_calls` logging, cost calculation.

   *Done when* changing `default_provider` in config reroutes calls without a restart.
3. **Auth + onboarding.** Cognito (mock JWT locally), resume upload, `parse_resume`, confirm screen, preferences, profile embedding.
4. **Ingestion.** Greenhouse, Lever, Ashby, Arbeitnow and HN adapters, company registry, normalize + embed, dedupe, `pipeline_runs`. Then Adzuna, Jooble and JSearch behind flags.
5. **Matching.** Filters, vector search, rerank, `parse_job` (cached), `score_fit`, matches UI.
6. **Contacts.** Waterfall, Hunter, verifier, cache, credit tracking.
7. **Drafting + approval.** `draft_email`, `check_claims`, approval UI, SES notifications.
8. **Sending.** Gmail and Outlook OAuth connect, token encryption, sender with every guardrail, `dry_run`, follow-ups, suppression list.
9. **Admin console.** All pages in section 10.
10. **AWS infrastructure.** CDK stacks: network, data, queues, services, edge, observability, AppConfig. Alarms and dashboard. GitHub Actions deploys to dev → staging → prod.
11. **Hardening.** Security review, data export and delete, load test, runbooks for each alarm.

## 16. What the owner must provide (ask when a phase needs it)

- **AWS:** account and region, with Bedrock model access to gpt-oss-20b in that region.
- **Google Cloud:** an OAuth client with the Gmail API enabled, scope `gmail.send`. Production needs Google app verification. While the consent screen is in "Testing" status, refresh tokens expire after 7 days.
- **Microsoft Entra:** an app registration with the delegated `Mail.Send` permission.
- **API keys:**
  - Hunter,
  - ZeroBounce or NeverBounce,
  - optional: Adzuna, Jooble, RapidAPI (JSearch), Apollo.
- **Domains:** a domain for the app, and an SES-verified sending domain for notifications.
- **Alerts:** an email address and optionally a Slack channel.
- **Legal:** a review of email and data-protection rules (for example CAN-SPAM, GDPR, India's DPDP Act) before opening to the public.