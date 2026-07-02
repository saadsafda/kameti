"""Cross-cutting helpers used across API modules."""

import re
import secrets

import frappe

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
AVATAR_TONES = ("clay", "plum", "teal", "green", "amber", "rust")
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def validate_e164(phone: str) -> None:
	if not isinstance(phone, str) or not E164_RE.match(phone):
		frappe.throw("Phone must be in E.164 format, e.g. +923123456789", frappe.ValidationError)


def email_for_phone(phone: str) -> str:
	return f"{phone[1:]}@kameti.local"


def phone_for_email(email: str) -> str:
	return "+" + email.split("@", 1)[0]


def user_name_for_phone(phone: str) -> str | None:
	return frappe.db.get_value("Kameti Profile", {"phone": phone}, "user")


def random_tone() -> str:
	return secrets.choice(AVATAR_TONES)


def initials_from(name: str) -> str:
	parts = [p for p in (name or "").split() if p]
	if not parts:
		return "??"
	if len(parts) == 1:
		return (parts[0][:2] or "??").upper()
	return (parts[0][:1] + parts[1][:1]).upper()


def short_code_from(title: str) -> str:
	return initials_from(title)


def mask_phone(phone: str | None) -> str:
	"""Mask an E.164 number, keeping the leading `+` and last 2 digits visible.

	Doesn't assume a fixed country-code length (PK/US are 1-3 digits, others
	up to 3), so it masks everything between the `+` and the last 2 digits
	rather than trying to split out a country code + area code.
	"""
	if not phone or not phone.startswith("+"):
		return phone or ""
	digits = phone[1:]
	if len(digits) < 7:
		return phone
	last2 = digits[-2:]
	hidden = len(digits) - 2
	return f"+{'•' * hidden}{last2}"


def new_invite_code() -> str:
	for _ in range(20):
		code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(6))
		if not frappe.db.exists("Kameti Committee", {"invite_code": code}):
			return code
	frappe.throw("Could not generate a unique invite code, please retry.")


def is_admin(kameti: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return frappe.db.get_value("Kameti Committee", kameti, "admin") == user


def require_admin(kameti: str, user: str | None = None) -> None:
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return
	if not is_admin(kameti, user):
		frappe.throw("Only the kameti admin can do this.", frappe.PermissionError)


def require_membership(kameti: str, user: str | None = None) -> "frappe._dict":
	user = user or frappe.session.user
	row = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": kameti, "user": user, "status": "active"},
		["name", "role", "display_name", "initials", "avatar_tone", "phone", "payout_month"],
		as_dict=True,
	)
	if not row:
		frappe.throw("You are not a member of this kameti.", frappe.PermissionError)
	return row


def require_membership_or_admin(kameti: str, user: str | None = None) -> None:
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return
	if is_admin(kameti, user):
		return
	if frappe.db.exists(
		"Kameti Membership",
		{"kameti": kameti, "user": user, "status": "active"},
	):
		return
	frappe.throw("You don't have access to this kameti.", frappe.PermissionError)


def require_session_user() -> str:
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw("Not authenticated.", frappe.AuthenticationError)
	return user
