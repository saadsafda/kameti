# Kameti API Reference

All endpoints live under `/api/method/kameti.<module>.<fn>` and follow Frappe's response envelope:

```json
{ "message": { ...payload... } }
```

On error, Frappe returns HTTP 4xx/5xx with:

```json
{ "exc_type": "ValidationError", "_server_messages": "[...]" }
```

## Authentication

After `verify_otp`, send the returned `api_key`/`api_secret` on every authenticated call:

```
Authorization: token <api_key>:<api_secret>
```

For multi-site benches also pass:

```
X-Frappe-Site-Name: kameti.site
```

## Conventions

- All times are ISO-8601 UTC unless noted.
- All phones must be **E.164** (`+923477401772` — not `03477401772`).
- POST endpoints expect `Content-Type: application/json`.
- "admin" = caller is the kameti's admin. "member" = caller has an active membership. "auth" = any signed-in user.

---

## 1. Auth (`kameti.api.auth`)

### POST `request_otp` — guest

Generates a 6-digit code and either (a) stores it plaintext in `Phone OTP` (provider=`console`), (b) sends via WhatsApp (provider=`whatsapp`), or (c) sends via SMS (provider=`sms`). Provider is set in **OTP Settings**.

**Request**
```json
{ "phone": "+923477401772", "purpose": "login" }
```
`purpose` ∈ `login` / `register` / `phone_change`.

**Response**
```json
{ "ok": true, "provider": "console", "expires_in": 300, "resend_after": 60 }
```

**Errors**: `ValidationError` (bad phone/purpose), `RateLimitExceededError` (1/60s or 5/hour).

### POST `check_otp` — guest

Verifies the code without doing the login flow. Consumes the OTP on success; bumps attempts on failure.

**Request**
```json
{ "phone": "+923477401772", "code": "655162", "purpose": "login" }
```

**Response**
```json
{ "ok": true }
```

**Errors**: `AuthenticationError` (wrong code or no active code), `ValidationError` (expired), `PermissionError` (locked after max attempts).

### POST `verify_otp` — guest

Verifies the code, creates the User if new, rotates and returns API keys.

**Request**
```json
{ "phone": "+923477401772", "code": "655162", "purpose": "login" }
```

**Response (existing user)**
```json
{
  "ok": true, "is_new_user": false, "user": "+923477401772",
  "api_key": "abcdef...", "api_secret": "xyz...",
  "profile": {
    "display_name": "Ayesha Khan", "urdu_name": "عائشہ خان",
    "language": "en", "dark_mode": false, "avatar_tone": "plum", "avatar_url": null
  }
}
```

**Response (new user)** — same shape but no `profile`. Client should route to RegisterScreen.

**Errors**: same as `check_otp`.

### POST `complete_registration` — auth

Called once after a new-user `verify_otp`.

**Request**
```json
{
  "display_name": "Ayesha Khan",
  "urdu_name": "عائشہ خان",
  "language": "en",
  "avatar_tone": "plum"
}
```
`urdu_name`, `language`, `avatar_tone` are optional. `language` ∈ `en`/`rom`/`ur`. `avatar_tone` ∈ `clay`/`plum`/`teal`/`green`/`amber`/`rust`.

**Response** — same `profile` block shape as `verify_otp`.

### POST `sign_out` — auth

Clears `api_key`/`api_secret` on the User, invalidating all sessions.

**Request**: `{}`
**Response**: `{ "ok": true }`

---

## 2. Hub (`kameti.api.hub`)

### GET `get_my_kametis` — auth

Single payload for the home screen.

**Response**
```json
{
  "total_owed_this_month": 35000,
  "owed_count": 3,
  "unread_notifications": 2,
  "kametis": [
    {
      "id": "KAM-0001",
      "title": "Eid Family Kameti",
      "urdu_title": "عید فیملی کمیٹی",
      "short_code": "EI",
      "tone": "clay",
      "role": "admin",
      "members_count": 10,
      "installment_amount": 5000,
      "current_month": 4,
      "duration_months": 10,
      "cycle_state": "active",
      "this_month_recipient": {
        "membership_id": "MEM-0004",
        "display_name": "Fatima Sheikh",
        "initials": "FS",
        "tone": "green",
        "amount": 45000
      },
      "paid_count": 6,
      "pending_count": 1,
      "unpaid_count": 3,
      "last_payer": { "initials": "FS", "tone": "green", "display_name": "Fatima Sheikh", "paid_at": "2026-05-18T14:32:00Z" }
    }
  ]
}
```

---

## 3. Profile (`kameti.api.profile`)

### GET `get_profile` — auth

**Response**
```json
{
  "display_name": "Ayesha Khan",
  "urdu_name": "عائشہ خان",
  "phone": "+923477401772",
  "language": "en",
  "dark_mode": false,
  "avatar_tone": "plum",
  "avatar_url": null,
  "joined_on": "2026-04-12T07:21:00Z",
  "kametis_count": 3
}
```

### POST `update_profile` — auth

Partial update. Send only the keys you want to change.

**Request**
```json
{ "display_name": "Ayesha Khan", "language": "ur", "dark_mode": true, "avatar_tone": "teal" }
```
Editable fields: `display_name`, `urdu_name`, `language`, `dark_mode`, `avatar_tone`, `avatar_image`.

**Response**: same shape as `get_profile`.

---

## 4. Kameti (`kameti.api.kameti`)

### POST `create_committee` — auth

Creates the committee, an admin Membership for the caller, all N Payout Slots, and any Payment Accounts in one transaction. Caller becomes the admin.

**Request**
```json
{
  "title": "Eid Family Kameti",
  "urdu_title": "عید فیملی کمیٹی",
  "members_count": 10,
  "installment_amount": 5000,
  "start_month": "2026-10-01",
  "tone": "clay",
  "payment_accounts": [
    { "method": "easypaisa", "account_number": "0312-345 6789", "account_title": "Ayesha Khan" },
    { "method": "bank", "account_number": "PK36 MEZN 0001 2345 6789", "account_title": "Ayesha Khan", "bank_name": "Meezan Bank" }
  ]
}
```
- `members_count` must be 4–30
- `installment_amount` > 0
- `start_month` is coerced to the 1st of that month
- `payment_accounts` is optional; method ∈ `easypaisa`/`jazzcash`/`bank`

**Response**
```json
{ "id": "KAM-0001", "invite_code": "K7H2Q9", "kameti": { /* same shape as a hub card */ } }
```

### GET `get_committee?kameti=<id>` — member

Returns the whole committee detail: members, roster, payment_accounts.

**Response (admin caller)** — full payload includes `invite_code`. Member callers receive everything except `invite_code` and member phones (only `phone_masked`).

```json
{
  "id": "KAM-0001",
  "title": "Eid Family Kameti",
  "urdu_title": null,
  "short_code": "EI",
  "tone": "clay",
  "admin": {
    "membership_id": "MEM-0001",
    "display_name": "Ali Raza",
    "initials": "AR",
    "tone": "plum",
    "phone_masked": "+92 312 ••• ••89"
  },
  "members_count": 10,
  "installment_amount": 5000,
  "total_pool": 50000,
  "start_month": "2026-10-01",
  "current_month": 4,
  "cycle_state": "active",
  "invite_code": "K7H2Q9",
  "members": [ /* see members.list */ ],
  "roster": [ /* see roster.get */ ],
  "payment_accounts": [ /* see pay.get_payment_methods */ ]
}
```

### POST `update_committee` — admin

Partial update.

**Request**
```json
{ "kameti": "KAM-0001", "title": "Eid Family Kameti 2026", "tone": "amber" }
```
Editable: `title`, `urdu_title`, `tone`, `installment_amount`, `start_month`, `wa_template_id`, `invite_expires_at`. `members_count` is editable only while `cycle_state = not_started`.

**Response**: `{ "ok": true }`

### POST `archive_committee` — admin

Soft-delete. Sets `archived=1`.

**Request**: `{ "kameti": "KAM-0001" }`
**Response**: `{ "ok": true }`

---

## 5. Join (`kameti.api.join`)

### GET `lookup_invite?code=<code>` — guest

Sanitized preview shown before the user joins.

**Response (valid)**
```json
{
  "valid": true,
  "kameti": {
    "title": "Eid Family Kameti",
    "short_code": "EI",
    "tone": "clay",
    "admin_name": "Ali Raza",
    "members_count": 10,
    "installment_amount": 5000,
    "total_pool": 50000,
    "duration_months": 10,
    "seats_left": 3
  }
}
```

**Response (invalid)**
```json
{ "valid": false, "reason": "expired" }
```
`reason` ∈ `not_found` / `expired` / `full` / `already_member`.

### POST `join_by_code` — auth

**Request**
```json
{ "code": "K7H2Q9", "display_name": "Ayesha Khan" }
```

**Response**
```json
{ "ok": true, "membership_id": "MEM-0011", "kameti_id": "KAM-0001" }
```

**Errors**: `ValidationError` for not_found / expired / already_member / full.

---

## 6. Members (`kameti.api.members`)

### GET `list?kameti=<id>&query=<text>` — member

`query` is optional; case-insensitive substring match against `display_name` and `urdu_name`.

**Response**
```json
{
  "members": [
    {
      "membership_id": "MEM-0001",
      "display_name": "Ali Raza",
      "urdu_name": "علی رضا",
      "initials": "AR",
      "tone": "clay",
      "role": "admin",
      "payout_month": 1,
      "phone_masked": "+92 312 ••• ••89",
      "phone": "+923123456789",
      "status": "active",
      "is_you": false,
      "joined_on": "2026-04-12T07:21:00Z",
      "this_month_status": "paid"
    }
  ]
}
```
`phone` is only included for admin callers. `this_month_status` ∈ `paid`/`pending`/`unpaid`/`receiver`.

### POST `add` — admin

If a User with that phone exists, links it. Otherwise creates a "shadow" membership (no `user`).

**Request**
```json
{
  "kameti": "KAM-0001",
  "display_name": "Hassan Malik",
  "urdu_name": "حسن ملک",
  "phone": "+923331122334",
  "payout_month": 5,
  "avatar_tone": "amber"
}
```
`urdu_name`, `payout_month`, `avatar_tone` are optional.

**Response**
```json
{ "membership_id": "MEM-0005", "is_existing_user": false, "invite_sent": false }
```

### POST `remove` — admin

Soft-removes. Clears their payout slot if assigned.

**Request**
```json
{ "membership_id": "MEM-0005", "reason": "Left the group" }
```

**Response**: `{ "ok": true }`

**Errors**: cannot remove the admin (transfer ownership first).

### POST `assign_payout_month` — admin

**Request**
```json
{ "membership_id": "MEM-0005", "payout_month": 7 }
```

**Response**: `{ "ok": true }`

**Errors**: `ValidationError` if the month is already assigned to another active member.

### POST `transfer_ownership` — admin

The new admin must be an active, app-linked member.

**Request**
```json
{ "kameti": "KAM-0001", "new_admin_membership_id": "MEM-0003" }
```

**Response**: `{ "ok": true }`

---

## 7. Roster (`kameti.api.roster`)

### GET `get?kameti=<id>` — member

**Response**
```json
{
  "slots": [
    {
      "month_index": 1,
      "month_date": "2026-10-01",
      "recipient": {
        "membership_id": "MEM-0001",
        "display_name": "Ali Raza",
        "initials": "AR",
        "tone": "clay"
      },
      "assignment_method": "manual",
      "assigned_on": "2026-09-30T19:00:00Z",
      "payout_amount": 45000,
      "status": "paid"
    },
    {
      "month_index": 4,
      "month_date": "2027-01-01",
      "recipient": null,
      "assignment_method": null,
      "assigned_on": null,
      "payout_amount": 45000,
      "status": "upcoming"
    }
  ]
}
```
`status` ∈ `upcoming`/`current`/`paid`/`skipped`.

### POST `assign_manual` — admin

**Request**
```json
{ "kameti": "KAM-0001", "month_index": 3, "membership_id": "MEM-0003" }
```

**Response**: `{ "ok": true }`

### POST `lucky_draw` — admin

Picks one unassigned member using `secrets.SystemRandom`. Records audit blob.

**Request**
```json
{ "kameti": "KAM-0001", "month_index": 4 }
```

**Response**
```json
{
  "winner": {
    "membership_id": "MEM-0004",
    "display_name": "Fatima Sheikh",
    "initials": "FS",
    "tone": "green"
  },
  "audit": {
    "eligible_membership_ids": ["MEM-0004", "MEM-0007", "MEM-0008", "MEM-0009"],
    "selected_index": 0,
    "seed_hash": "sha256:..."
  }
}
```
The client uses `eligible_membership_ids` ordering for the wheel animation so it matches the audited outcome.

**Errors**: `ValidationError` when no eligible members or month already assigned.

### POST `unassign` — admin

Clears a slot.

**Request**
```json
{ "kameti": "KAM-0001", "month_index": 4 }
```

**Response**: `{ "ok": true }`

---

## 8. Dashboard (`kameti.api.dashboard`)

### GET `get_admin_dashboard?kameti=<id>` — admin

**Response**
```json
{
  "cycle_state": "active",
  "current_month": 4,
  "current_month_label": "Jan 2027",
  "due_on": "2027-01-05",
  "this_month_recipient": { /* membership_id, display_name, initials, tone, amount */ },
  "collected": 30000,
  "total_due": 45000,
  "paid_of": "6/9",
  "members": [
    { "membership_id": "MEM-0001", "display_name": "Ali Raza", "initials": "AR", "tone": "clay", "amount": 5000, "status": "paid" },
    { "membership_id": "MEM-0004", "display_name": "Fatima Sheikh", "initials": "FS", "tone": "green", "amount": 45000, "status": "receiver" }
  ],
  "unpaid_member_ids": ["MEM-0008", "MEM-0009", "MEM-0010"],
  "pending_approvals_count": 1
}
```
`status` ∈ `paid`/`pending`/`unpaid`/`receiver`.

### GET `get_member_dashboard?kameti=<id>` — member

**Response**
```json
{
  "you": { "membership_id": "MEM-0002", "display_name": "Ayesha Khan", "initials": "AK", "tone": "plum" },
  "this_installment": {
    "amount": 5000,
    "status": "unpaid",
    "due_on": "2027-01-05",
    "days_left": 2,
    "existing_payment_id": null
  },
  "your_payout": {
    "month_index": 7,
    "month_label": "Apr 2027",
    "amount": 45000
  },
  "progress": { "current": 4, "total": 10 },
  "this_month_recipient": { /* same shape as admin */ },
  "schedule_preview": [
    { "month_index": 4, "membership_id": "MEM-0004", "display_name": "Fatima Sheikh", "label": "now" },
    { "month_index": 5, "membership_id": "MEM-0005", "display_name": "Hassan Malik" },
    { "month_index": 6, "membership_id": "MEM-0006", "display_name": "Zainab Iqbal" }
  ]
}
```

---

## 9. Pay (`kameti.api.pay`)

### GET `get_payment_methods?kameti=<id>` — member

**Response**
```json
{
  "admin_name": "Ali Raza",
  "accounts": [
    { "id": "PA-0001", "method": "easypaisa", "account_number": "0312-345 6789", "account_title": "Ali Raza", "bank_name": null },
    { "id": "PA-0003", "method": "bank", "account_number": "PK36 MEZN 0001 2345 6789", "account_title": "Ali Raza", "bank_name": "Meezan Bank" }
  ]
}
```

### Receipt upload (Frappe built-in)

```
POST /api/method/upload_file
Content-Type: multipart/form-data
  file:        <jpg/png ≤ 5MB>
  is_private:  1
  doctype:     Installment Payment
```
Returns `{ message: { file_url: "/private/files/xyz.jpg", ... } }`. Send the `file_url` as `receipt_file` in the next call.

### POST `submit_payment` — member

**Request**
```json
{
  "kameti": "KAM-0001",
  "payout_month": 4,
  "amount": 5000,
  "method": "easypaisa",
  "payment_account": "PA-0001",
  "receipt_file": "/private/files/xyz.jpg",
  "transaction_id": "EP72839102",
  "notes": null,
  "voice_note": null
}
```
- `method` ∈ `easypaisa`/`jazzcash`/`bank`/`cash`/`other`
- `amount` must equal the committee's `installment_amount` (±0.01)
- `receipt_file` is required unless `method = cash`

**Response**
```json
{ "payment_id": "PAY-0033", "status": "pending", "awaiting_approval": true }
```

**Errors**: `ValidationError` on amount mismatch, missing receipt, duplicate active payment for `(kameti, payer, payout_month)`.

### GET `get_payment?payment_id=<id>` — payer or admin

**Response**
```json
{
  "payment_id": "PAY-0033",
  "kameti": "KAM-0001",
  "payer": { "display_name": "Ayesha Khan", "initials": "AK", "tone": "plum" },
  "amount": 5000,
  "method": "easypaisa",
  "transaction_id": "EP72839102",
  "receipt_url": "/private/files/xyz.jpg",
  "status": "pending",
  "submitted_on": "2027-01-02T14:35:00Z",
  "reviewed_on": null,
  "rejection_reason": null,
  "admin": { "display_name": "Ali Raza", "online": true }
}
```
`online` = admin's `last_active` within the last 2 minutes.

### GET `get_approval_queue?kameti=<id>` — admin

**Response**
```json
{
  "queue": [
    {
      "payment_id": "PAY-0033",
      "payer": { "display_name": "Usman Tariq", "initials": "UT", "tone": "clay" },
      "amount": 5000,
      "method": "easypaisa",
      "transaction_id": "EP72938",
      "receipt_url": "/private/files/uploaded.jpg",
      "submitted_on": "2027-01-02T14:35:00Z",
      "time_ago": "2 minutes ago"
    }
  ],
  "count": 3
}
```

### POST `approve_payment` — admin

**Request**: `{ "payment_id": "PAY-0033" }`
**Response**: `{ "ok": true }`

Sets `status=approved`, emits a `payment_approved` activity to the payer.

### POST `reject_payment` — admin

**Request**
```json
{ "payment_id": "PAY-0033", "reason": "Amount doesn't match" }
```
**Response**: `{ "ok": true }`

Sets `status=rejected`, stores `rejection_reason`, emits a `payment_rejected` activity to the payer.

---

## 10. Reminders (`kameti.api.reminders`)

### GET `get_template?kameti=<id>&membership_id=<id>&channel=<ch>&language=<lang>` — admin

`channel` ∈ `sms`/`whatsapp`/`push`/`in_app`. `language` ∈ `en`/`rom`/`ur` (default `en`).

**Response**
```json
{
  "language": "en",
  "channel": "whatsapp",
  "subject": null,
  "body": "Hi Usman Tariq,\nYour installment of Rs. 5,000 for Eid Family Kameti is due by 5 Nov.\nEasypaisa 0312-345 6789 (Ali Raza) — upload the screenshot in the app."
}
```

### POST `send` — admin

Queues reminders. The hourly `dispatch_reminder_queue` job actually sends them.

**Request**
```json
{
  "kameti": "KAM-0001",
  "membership_ids": ["MEM-0007", "MEM-0008", "MEM-0009"],
  "channel": "whatsapp",
  "language": "en",
  "override_body": null
}
```
If `override_body` is set, it replaces the rendered template for all recipients.

**Response**
```json
{ "queued": 3, "reminder_ids": ["REM-0101", "REM-0102", "REM-0103"] }
```

### GET `recent?kameti=<id>` — admin

Last 50 reminders for this kameti.

**Response**
```json
{
  "items": [
    {
      "id": "REM-0101",
      "member": { "membership_id": "MEM-0007", "display_name": "Usman Tariq" },
      "channel": "whatsapp",
      "status": "sent",
      "sent_at": "2027-01-02T14:38:00Z"
    }
  ]
}
```

---

## 11. Notifications (`kameti.api.notifications`)

### GET `list?limit=20&before=<iso8601>` — auth

Both query params optional. `limit` capped to 100.

**Response**
```json
{
  "unread_count": 2,
  "items": [
    {
      "id": "ACT-0901",
      "type": "payment_approved",
      "title": "Approved",
      "body": "Ali Raza approved your Rs. 5,000 payment.",
      "kameti_id": "KAM-0001",
      "payload": { "payment_id": "PAY-0033" },
      "is_read": false,
      "created": "2027-01-02T15:01:00Z"
    }
  ]
}
```
`type` ∈ `payment_pending`/`payment_approved`/`payment_rejected`/`reminder_received`/`draw_complete`/`member_joined`/`cycle_complete`/`announcement`.

### POST `mark_read` — auth

Mark specific IDs:
```json
{ "ids": ["ACT-0901", "ACT-0902"] }
```

Or mark all:
```json
{ "ids": "all" }
```

**Response**: `{ "ok": true }`

### POST `register_fcm` — auth

**Request**: `{ "token": "fcm-token-string" }`
**Response**: `{ "ok": true }`

---

## 12. Settings (`kameti.api.settings`)

### POST `update` — auth

Shortcut for the two common settings.

**Request**
```json
{ "language": "ur", "dark_mode": true }
```
**Response**: same as `profile.get_profile`.

---

## 13. Admin: OTP Settings (Single DocType)

Open in the desk: `/app/otp-settings`. Fields:

| Field | Type | Default | Notes |
|---|---|---|---|
| `provider` | Select | `console` | `console` / `whatsapp` / `sms` |
| `use_fixed_dev_code` | Check | 0 | When on, every console-mode OTP is the fixed code below |
| `fixed_dev_code` | Data | `472901` | Only used when the toggle above is on |
| `otp_ttl_seconds` | Int | 300 | Validity in seconds |
| `max_attempts` | Int | 5 | Lockout after this many wrong tries |
| `resend_interval_seconds` | Int | 60 | Minimum gap between two `request_otp` for the same phone |
| `hourly_limit` | Int | 5 | Maximum `request_otp` per phone per hour |
| `whatsapp_backend` | Select | `meta` | `meta` / `vonage` / `ultramsg` |
| `whatsapp_phone_number_id` | Data |  | Meta Cloud API phone number ID |
| `whatsapp_access_token` | Password |  | Meta Cloud API access token |
| `whatsapp_api_version` | Data | `v18.0` | Meta Graph API version |
| `whatsapp_otp_template_name` | Data | `kameti_otp` | Meta OTP template name |
| `whatsapp_otp_template_lang` | Data | `en` | Meta OTP template language |
| `ultramsg_instance_id` | Data |  | UltraMsg instance ID |
| `ultramsg_token` | Password |  | UltraMsg instance token |
| `ultramsg_base_url` | Data | `https://api.ultramsg.com` | UltraMsg API base URL |
| `ultramsg_priority` | Int | `10` | UltraMsg message priority |

In `console` mode the plaintext lives in `Phone OTP.code` — readable at `/app/phone-otp`.

---

## 14. Edge cases the client must handle

| Endpoint | Edge case | HTTP | exc_type |
|---|---|---|---|
| `auth.verify_otp` | Wrong code | 401 | `AuthenticationError` |
| `auth.verify_otp` | 5 wrong codes | 403 | `PermissionError` |
| `auth.verify_otp` | OTP expired | 410 | `ValidationError` |
| `auth.request_otp` | < 60s since last request | 429 | `RateLimitExceededError` |
| `auth.request_otp` | > 5 requests in an hour | 429 | `RateLimitExceededError` |
| `pay.submit_payment` | Amount ≠ installment_amount | 400 | `ValidationError` |
| `pay.submit_payment` | Existing pending payment | 409 | `ValidationError` |
| `pay.approve_payment` | Caller not admin | 403 | `PermissionError` |
| `join.join_by_code` | Already a member | 400 | `ValidationError` |
| `members.add` | Phone already in this kameti | 400 | `ValidationError` |
| `roster.assign_manual` | Month already assigned | 400 | `ValidationError` |
| `roster.lucky_draw` | No eligible members | 400 | `ValidationError` |
| `kameti.update_committee` | Editing `members_count` after cycle started | 400 | `ValidationError` |

---

## 15. Mapping: mobile screen → endpoints

| Screen | Endpoints |
|---|---|
| `SplashScreen` | none |
| `PhoneScreen` | `auth.request_otp` |
| `OtpScreen` | `auth.verify_otp` |
| `RegisterScreen` | `auth.complete_registration`, `upload_file` |
| `MyKametisScreen` | `hub.get_my_kametis`, `notifications.list` |
| `ProfileScreen` | `profile.get_profile`, `profile.update_profile`, `auth.sign_out` |
| `SettingsScreen` | `settings.update` |
| `CreateCommitteeScreen` | `kameti.create_committee` |
| `RosterScreen` | `roster.get`, `roster.assign_manual` |
| `LuckyDrawScreen` | `roster.lucky_draw` |
| `DashboardScreen` (admin) | `dashboard.get_admin_dashboard`, `reminders.send` |
| `MemberViewScreen` | `dashboard.get_member_dashboard` |
| `PayHowtoScreen` | `pay.get_payment_methods` |
| `UploadProofScreen` | `upload_file`, `pay.submit_payment` |
| `AwaitingScreen` | `pay.get_payment` (poll every 5s, max 1 min) |
| `ApprovalQueueScreen` | `pay.get_approval_queue`, `pay.approve_payment`, `pay.reject_payment` |
| `WhatsappShareScreen` | `reminders.get_template`, `reminders.send` |
| `JoinKametiScreen` | `join.lookup_invite`, `join.join_by_code` |
| `MembersManagementScreen` | `members.list`, `members.add`, `members.remove`, `members.assign_payout_month` |
| Bottom tab — Members | `members.list` |
| Bottom tab — Schedule | `roster.get` |
| Bottom tab — Activity | `notifications.list`, `notifications.mark_read` |

---

## 16. Curl smoke-test (kameti.site, dev)

```bash
# Provider = console (default) — code is stored in Phone OTP, not sent.
curl -X POST localhost:8000/api/method/kameti.api.auth.request_otp \
  -H 'X-Frappe-Site-Name: kameti.site' -H 'Content-Type: application/json' \
  -d '{"phone":"+923477401772","purpose":"login"}'

# Read the code in MariaDB:
bench --site kameti.site mariadb \
  -e "SELECT code FROM \`tabPhone OTP\` WHERE phone='+923477401772' ORDER BY creation DESC LIMIT 1;"

# Or check the code (no login) — confirms the OTP flow:
curl -X POST localhost:8000/api/method/kameti.api.auth.check_otp \
  -H 'X-Frappe-Site-Name: kameti.site' -H 'Content-Type: application/json' \
  -d '{"phone":"+923477401772","code":"<CODE>","purpose":"login"}'

# Or full login + get keys:
curl -X POST localhost:8000/api/method/kameti.api.auth.verify_otp \
  -H 'X-Frappe-Site-Name: kameti.site' -H 'Content-Type: application/json' \
  -d '{"phone":"+923477401772","code":"<CODE>","purpose":"login"}'

# Use the keys:
curl localhost:8000/api/method/kameti.api.hub.get_my_kametis \
  -H 'X-Frappe-Site-Name: kameti.site' \
  -H 'Authorization: token <api_key>:<api_secret>'
```
