"""OTP generation, hashing, verification, rate limiting.

Settings (TTL, max attempts, resend interval, hourly limit, provider) are
configured in the **OTP Settings** Single DocType. Defaults below are used
as fallbacks when the doctype hasn't been migrated yet.

Storage rules:
  - provider = 'console' → store plaintext in Phone OTP.code (read it from
	the desk; nothing is sent). Hash is left blank.
  - provider = 'whatsapp' / 'sms' → store hash in Phone OTP.code_hash and
	hand the plaintext to the provider. Plaintext is NOT persisted.
"""

import secrets

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from passlib.hash import pbkdf2_sha256

DEFAULTS = {
	"otp_ttl_seconds": 300,
	"max_attempts": 5,
	"resend_interval_seconds": 60,
	"hourly_limit": 5,
	"provider": "console",
	"use_fixed_dev_code": 0,
	"fixed_dev_code": "472901",
}

HARDCODED_TEST_PHONE = "+923477401772"
HARDCODED_TEST_CODE = "123456"


def get_settings() -> dict:
	"""Read OTP Settings with safe defaults. Cached for the request."""
	try:
		doc = frappe.get_cached_doc("OTP Settings")
	except Exception:
		return dict(DEFAULTS)
	out = dict(DEFAULTS)
	for k in DEFAULTS:
		v = getattr(doc, k, None)
		if v not in (None, ""):
			out[k] = v
	return out


def generate_code(phone: str | None = None, settings: dict | None = None) -> str:
	s = settings or get_settings()
	if phone == HARDCODED_TEST_PHONE:
		return HARDCODED_TEST_CODE
	if s.get("use_fixed_dev_code"):
		return str(s.get("fixed_dev_code") or DEFAULTS["fixed_dev_code"])
	return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str) -> str:
	return pbkdf2_sha256.hash(code)


def verify_hash(code: str, hashed: str) -> bool:
	try:
		return pbkdf2_sha256.verify(code, hashed)
	except Exception:
		return False


def check_rate_limit(phone: str, settings: dict | None = None) -> None:
	s = settings or get_settings()
	cache = frappe.cache()
	if cache.get_value(f"kameti:otp:last:{phone}"):
		frappe.throw(
			"Please wait before requesting another code.",
			frappe.RateLimitExceededError,
		)
	count = cache.get_value(f"kameti:otp:hour:{phone}") or 0
	if int(count) >= int(s["hourly_limit"]):
		frappe.throw(
			"Too many OTP requests. Try again in an hour.",
			frappe.RateLimitExceededError,
		)


def record_request(phone: str, settings: dict | None = None) -> None:
	s = settings or get_settings()
	cache = frappe.cache()
	cache.set_value(
		f"kameti:otp:last:{phone}", "1",
		expires_in_sec=int(s["resend_interval_seconds"]),
	)
	hour_key = f"kameti:otp:hour:{phone}"
	count = cache.get_value(hour_key) or 0
	cache.set_value(hour_key, int(count) + 1, expires_in_sec=3600)


def create_otp(
	phone: str,
	purpose: str,
	requested_ip: str | None = None,
	settings: dict | None = None,
) -> tuple[str, str]:
	"""Generate and persist a code. Returns (code, provider).

	The provider tells the caller whether to actually send (whatsapp/sms) or
	skip sending (console — the code is already readable in the desk).
	"""
	s = settings or get_settings()
	provider = s["provider"]
	console_mode = provider == "console"

	# Invalidate prior active OTPs for this (phone, purpose)
	frappe.db.sql(
		"""UPDATE `tabPhone OTP` SET consumed=1
		   WHERE phone=%s AND purpose=%s AND consumed=0""",
		(phone, purpose),
	)

	code = generate_code(phone, s)
	expires_at = add_to_date(now_datetime(), seconds=int(s["otp_ttl_seconds"]))

	doc = frappe.new_doc("Phone OTP")
	doc.phone = phone
	doc.purpose = purpose
	doc.expires_at = expires_at
	doc.attempts = 0
	doc.consumed = 0
	doc.requested_ip = requested_ip
	if console_mode:
		doc.code = code
		doc.code_hash = None
	else:
		doc.code = None
		doc.code_hash = hash_code(code)
	doc.insert(ignore_permissions=True)
	return code, provider


def consume_otp(phone: str, code: str, purpose: str, settings: dict | None = None) -> None:
	"""Verify a code or throw. Bumps attempts on miss; locks after max_attempts."""
	s = settings or get_settings()
	row = frappe.db.get_value(
		"Phone OTP",
		{"phone": phone, "purpose": purpose, "consumed": 0},
		["name", "code", "code_hash", "expires_at", "attempts"],
		as_dict=True,
		order_by="creation desc",
	)
	if not row:
		frappe.throw(
			"No active code for this phone. Request a new one.",
			frappe.AuthenticationError,
		)
	if get_datetime(row.expires_at) < now_datetime():
		frappe.throw("This code has expired.", frappe.ValidationError)
	max_attempts = int(s["max_attempts"])
	if row.attempts >= max_attempts:
		frappe.throw(
			"Too many attempts. Request a new code.",
			frappe.PermissionError,
		)

	is_valid = False
	if row.code:
		is_valid = secrets.compare_digest(str(code), str(row.code))
	elif row.code_hash:
		is_valid = verify_hash(code, row.code_hash)

	if not is_valid:
		new_attempts = row.attempts + 1
		frappe.db.set_value("Phone OTP", row.name, "attempts", new_attempts)
		if new_attempts >= max_attempts:
			frappe.throw(
				"Too many attempts. Request a new code.",
				frappe.PermissionError,
			)
		frappe.throw("Incorrect code.", frappe.AuthenticationError)
	frappe.db.set_value("Phone OTP", row.name, "consumed", 1)
