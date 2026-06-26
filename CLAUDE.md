# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Frappe v15 app — the backend for a Flutter Kameti (rotating savings / ROSCA) mobile app. The Flutter side never uses Frappe's REST CRUD: every screen calls a hand-shaped endpoint under `/api/method/kameti.api.<module>.<fn>`. See [API.md](API.md) for the per-endpoint request/response reference.

Bench root is `/Users/mac/frappe/frappe-bench`. Default site is `kameti.site` (set in `sites/common_site_config.json`); `developer_mode` is on at the bench level.

## Common commands

All bench commands run from the bench root, not this app's directory:

```bash
# Apply DocType JSON / fixture changes after editing
bench --site kameti.site migrate

# Run the dev server (web on :8000, socketio on :9000)
bench start

# Python REPL with Frappe + the site already booted
bench --site kameti.site console

# Direct MariaDB shell — useful for inspecting Phone OTP.code in console-mode OTP testing
bench --site kameti.site mariadb

# Tail the app's logger output
tail -f sites/<site>/logs/kameti.log

# Lint (ruff + eslint + prettier + pyupgrade via pre-commit)
pre-commit run --all-files

# Tests (none authored yet; runner is wired)
bench --site kameti.site run-tests --app kameti
```

When calling HTTP endpoints against a multi-site bench, always pass `X-Frappe-Site-Name: kameti.site` because `default_site` is shared across sites in this bench.

## Architecture

### Layout

- `kameti/api/*.py` — one file per logical area (auth, hub, kameti, join, members, roster, dashboard, pay, reminders, notifications, settings, profile, permission). Every public endpoint is `@frappe.whitelist`-decorated and shapes its own response. Helper `_activity()` is duplicated across modules that emit notifications — keep them in sync.
- `kameti/kameti/doctype/<snake_name>/` — DocType definitions (JSON schema + a `Document` subclass). The full set: `kameti_profile`, `kameti_committee`, `kameti_membership`, `payout_slot`, `installment_payment`, `payment_account`, `phone_otp`, `reminder`, `activity`, `otp_settings`.
- `kameti/utils/` — cross-cutting helpers. `common.py` has phone validation, masking, initials, invite-code generation, and the `require_admin` / `require_membership` / `require_session_user` guards used by every endpoint. `otp.py` is the OTP state machine. `whatsapp.py` is a pluggable provider (Meta Cloud API or Vonage). `sms.py` is Twilio. `lucky_draw.py` and `templates.py` are pure helpers.
- `kameti/tasks.py` — five scheduled jobs wired in `hooks.py`.
- `kameti/install.py` — `after_install` creates the `Kameti Member` and `Kameti Admin` roles.

### Identity model

Phone is the source of truth, but Frappe needs a `User`. The mapping is deterministic:

- `User.email = f"{phone[1:]}@kameti.local"` (digits only, no leading `+`)
- `User.name == User.email` (Frappe convention)
- `Kameti Profile.phone` (unique) is the lookup index — go `phone → Kameti Profile → user`
- `common.phone_for_email("923...@kameti.local")` reverses this
- `frappe.session.user` returns `User.name` (the email-shaped string), **not** the phone

Every API-created user gets BOTH the `Kameti Member` and `Kameti Admin` roles at insert time. Role-level permissions are the **maximum**; row-level scoping happens through `permission_query_conditions` and `has_permission` in `api/permission.py` (registered in `hooks.py`). Access rule for all kameti-scoped DocTypes: caller is either the kameti's `admin` OR has an active `Kameti Membership` row. SQL fragments returned by the `*_query` functions are injected raw into list queries — always go through `frappe.db.escape()` for the user value.

### OTP flow

State lives in two places: the `Phone OTP` DocType (one row per request, attempts counter, consumed flag) and Frappe cache (rate-limit counters keyed by phone). The flow is:

1. `auth.request_otp` → `otp.create_otp` invalidates prior unconsumed rows for the same `(phone, purpose)`, persists a new row, then routes the send based on the **provider** field in the `OTP Settings` Single DocType.
2. `auth.verify_otp` (login + key rotation) or `auth.check_otp` (verify only) → `otp.consume_otp` validates the latest row by plaintext or hash, increments attempts on miss, sets `consumed=1` on success.

Provider modes (set in `/app/otp-settings`):

- `console` — default. Stores plaintext in `Phone OTP.code`; nothing is sent. Read it in the desk at `/app/phone-otp` or via the mariadb shell. This is the production-safe stand-in until WhatsApp/Twilio credentials exist.
- `whatsapp` — calls `whatsapp.send_otp(phone, code)`. `whatsapp.py` chooses the backend from `OTP Settings.whatsapp_backend` (with a legacy `site_config.json` fallback): `meta` (Cloud API with an authentication template) or `vonage` (Messages API / Sandbox - free-form text because the sandbox has no template approval). Meta Cloud API credentials live in `OTP Settings`.
- `sms` — calls `sms.send_sms` (Twilio).

Switching providers requires zero code changes — just edit `OTP Settings` (and any legacy `site_config.json` values only if you still rely on them). In console mode `Phone OTP.code_hash` is blank; in `whatsapp`/`sms` mode `code` is blank and only the hash is stored.

Other OTP knobs in `OTP Settings`: TTL, max attempts, resend interval, hourly limit. Defaults live in `utils/otp.py:DEFAULTS` and are used as a fallback when the DocType doesn't exist yet (fresh install before migrate).

### Permissions

`hooks.py` registers `permission_query_conditions` + `has_permission` for 7 DocTypes. Both call into `api/permission.py`. System Manager always passes; otherwise the rule is admin-of-kameti OR active-member-of-kameti, with the exception of `Activity` (recipient-only) and `Installment Payment` (payer's user OR kameti admin).

### Committee creation

`kameti.create_committee` is the only place a `Kameti Committee` is born. In one transaction it creates: the committee, the admin's own `Kameti Membership`, all N empty `Payout Slot` rows, any `Payment Account` rows from the request, plus any roster the admin pre-fills.

The roster pre-fill takes `members: [{display_name, phone?, urdu_name?, payout_months?}]`. `payout_months` is a **list** of month indices — a single person can hold multiple shares (the "double share" case), and `_claim()` validates that no two slots collide before any DB write. Members added without a phone (or with a phone that has no User yet) are "ledger" memberships with `user=None`; they auto-link on first OTP login with that phone.

### Scheduled tasks

Wired in `hooks.py`, implemented in `tasks.py`:

- `advance_current_month` (cron `0 0 1 * *`) — activates `not_started` committees whose `start_month` has arrived; bumps `current_month` on active ones and flips the prior slot to `paid`, new slot to `current`; marks the cycle `completed` when past the last month.
- `dispatch_reminder_queue` (hourly) — drains `Reminder` rows with `status=queued`, calls `sms.send_sms` or `whatsapp.send_text`, stamps `status`/`provider_id`/`error`.
- `expire_otps` (daily) — hard-deletes `Phone OTP` rows older than 24h.
- `send_due_reminders` (daily) — three days before the 5th of the month, queues an SMS row for each unpaid non-recipient.
- `archive_completed_kametis` (daily) — sets `cycle_state=completed` when every Payout Slot is `paid`/`skipped`.

## Conventions worth knowing

- **Phone format**: every endpoint that takes a phone runs `common.validate_e164()` — Pakistani numbers must be normalised client-side (`03477401772` → `+923477401772`).
- **Initials & tones**: derived consistently via `common.initials_from()` and `common.AVATAR_TONES`. Don't reinvent these inline.
- **Phone OTP.code_hash is `Data`, not `Password`** — deviation from the original spec. bcrypt-then-AES is double-encryption with no security gain; passlib's `pbkdf2_sha256` is used directly.
- **bcrypt is NOT installed** in the bench venv — use `passlib.hash.pbkdf2_sha256` instead. The bcrypt backend of passlib also isn't installed for the same reason.
- **Multi-site bench**: every curl call needs `X-Frappe-Site-Name: kameti.site`. Forgetting this hits whatever `default_site` resolves to.
- **Dev OTP is not a fixed code by default** — in console mode `OTP Settings.use_fixed_dev_code` decides. If on, every code is `fixed_dev_code` (defaults to `472901`). If off, every code is random and you must read it from `Phone OTP`.
- **API key rotation**: `verify_otp` invalidates the prior `api_key`/`api_secret` every time. Logging in on a new device kicks the old session.
- **Linting**: `pyproject.toml` configures ruff with `line-length=110`, target `py310`. Tabs for indentation across the codebase (not spaces) — `.editorconfig` enforces this. `pre-commit` runs ruff/eslint/prettier/pyupgrade.
- **Reference doc**: [API.md](API.md) is the authoritative endpoint reference and includes the curl smoke-test flow.
