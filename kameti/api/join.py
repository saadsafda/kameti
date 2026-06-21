"""Invite-code join flow. See spec §6."""

import frappe
from frappe.utils import get_datetime, now_datetime

from kameti.utils import common


@frappe.whitelist(methods=["GET"])
def lookup_invite(code: str):
	if not code or not isinstance(code, str):
		return {"valid": False, "reason": "not_found"}
	code = code.strip().upper()
	committee = frappe.db.get_value(
		"Kameti Committee",
		{"invite_code": code, "archived": 0},
		["name", "title", "short_code", "tone", "admin", "members_count",
		 "installment_amount", "duration_months", "invite_expires_at"],
		as_dict=True,
	)
	if not committee:
		return {"valid": False, "reason": "not_found"}
	if committee.invite_expires_at and get_datetime(committee.invite_expires_at) < now_datetime():
		return {"valid": False, "reason": "expired"}

	active = frappe.db.count(
		"Kameti Membership", {"kameti": committee.name, "status": "active"},
	)
	if active >= committee.members_count:
		return {"valid": False, "reason": "full"}

	user = frappe.session.user
	if user and user != "Guest" and frappe.db.exists(
		"Kameti Membership",
		{"kameti": committee.name, "user": user, "status": "active"},
	):
		return {"valid": False, "reason": "already_member"}

	admin_name = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": committee.name, "user": committee.admin, "role": "admin", "status": "active"},
		"display_name",
	) or "Admin"

	return {
		"valid": True,
		"kameti": {
			"title": committee.title,
			"short_code": committee.short_code,
			"tone": committee.tone,
			"admin_name": admin_name,
			"members_count": committee.members_count,
			"installment_amount": committee.installment_amount,
			"total_pool": (committee.installment_amount or 0) * committee.members_count,
			"duration_months": committee.duration_months,
			"seats_left": committee.members_count - active,
		},
	}


@frappe.whitelist(methods=["POST"])
def join_by_code(code: str, display_name: str):
	user = common.require_session_user()
	if not code:
		frappe.throw("Invite code is required.", frappe.ValidationError)
	if not display_name or not display_name.strip():
		frappe.throw("Display name is required.", frappe.ValidationError)

	code = code.strip().upper()
	committee = frappe.db.get_value(
		"Kameti Committee",
		{"invite_code": code, "archived": 0},
		["name", "admin", "members_count", "invite_expires_at"],
		as_dict=True,
	)
	if not committee:
		frappe.throw("Invite code not found.", frappe.ValidationError)
	if committee.invite_expires_at and get_datetime(committee.invite_expires_at) < now_datetime():
		frappe.throw("This invite code has expired.", frappe.ValidationError)

	if frappe.db.exists(
		"Kameti Membership",
		{"kameti": committee.name, "user": user, "status": "active"},
	):
		frappe.throw(
			"You are already a member of this kameti.", frappe.ValidationError,
		)

	active = frappe.db.count(
		"Kameti Membership", {"kameti": committee.name, "status": "active"},
	)
	if active >= committee.members_count:
		frappe.throw("This kameti is full.", frappe.ValidationError)

	profile = frappe.db.get_value(
		"Kameti Profile", {"user": user},
		["urdu_name", "phone", "avatar_tone"], as_dict=True,
	) or frappe._dict(urdu_name=None, phone="", avatar_tone="clay")

	display_name = display_name.strip()[:60]
	mem = frappe.new_doc("Kameti Membership")
	mem.kameti = committee.name
	mem.user = user
	mem.display_name = display_name
	mem.urdu_name = profile.urdu_name
	mem.phone = profile.phone or ""
	mem.initials = common.initials_from(display_name)
	mem.avatar_tone = profile.avatar_tone or common.random_tone()
	mem.role = "member"
	mem.status = "active"
	mem.joined_on = now_datetime()
	mem.insert(ignore_permissions=True)

	_activity(
		recipient=committee.admin,
		kameti=committee.name,
		type_="member_joined",
		title=f"{display_name} joined",
		body="A new member joined your kameti.",
		payload={"membership_id": mem.name},
	)
	frappe.db.commit()
	return {"ok": True, "membership_id": mem.name, "kameti_id": committee.name}


def _activity(recipient, kameti, type_, title, body, payload=None):
	a = frappe.new_doc("Activity")
	a.recipient = recipient
	a.kameti = kameti
	a.type = type_
	a.title = title
	a.body = body
	a.payload = frappe.as_json(payload) if payload else None
	a.is_read = 0
	a.insert(ignore_permissions=True)
