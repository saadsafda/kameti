"""Kameti committee CRUD. See spec §5."""

import frappe
from frappe.utils import add_months, getdate, now_datetime

from kameti.utils import common


@frappe.whitelist(methods=["POST"])
def create_committee(
	title: str,
	members_count: int,
	installment_amount: float,
	start_month: str,
	urdu_title: str | None = None,
	tone: str = "clay",
	payment_accounts: list | None = None,
	members: list | None = None,
	admin_payout_months: list | None = None,
	admin_payout_month: int | None = None,
):
	"""Create a committee, the admin membership, N empty payout slots, and
	(optionally) the rest of the roster in one shot.

	`members` is the ordered list of people the admin adds up front — each is
	`{display_name, phone?, urdu_name?, payout_months?}`. A person may hold
	more than one slot/share, so `payout_months` is a LIST of month indices —
	one entry per share they hold. Months left unclaimed stay open for the
	lucky draw. `admin_payout_months` pins the admin's own month(s).

	People added by name only (no phone, or a phone with no account yet) are
	"ledger" members; when they later sign up with the same phone their
	membership auto-links.
	"""
	user = common.require_session_user()

	members_count = int(members_count)
	installment_amount = float(installment_amount)
	if not (4 <= members_count <= 30):
		frappe.throw("members_count must be between 4 and 30.", frappe.ValidationError)
	if installment_amount <= 0:
		frappe.throw("installment_amount must be > 0.", frappe.ValidationError)
	if not title or not title.strip():
		frappe.throw("Title is required.", frappe.ValidationError)

	members = members or []

	def _as_months(value_list, single) -> list[int]:
		if value_list:
			return [int(x) for x in value_list]
		if single not in (None, "", 0):
			return [int(single)]
		return []

	# --- validate the roster the admin built before we write anything -----
	used_months: set[int] = set()

	def _claim(months: list[int]) -> list[int]:
		cleaned = []
		for pm in months:
			pm = int(pm)
			if not (1 <= pm <= members_count):
				frappe.throw(
					f"Payout month {pm} is outside 1–{members_count}.",
					frappe.ValidationError,
				)
			if pm in used_months:
				frappe.throw(
					f"Month {pm} is assigned to more than one slot.",
					frappe.ValidationError,
				)
			used_months.add(pm)
			cleaned.append(pm)
		return cleaned

	admin_months = _claim(_as_months(admin_payout_months, admin_payout_month))

	clean_members = []
	seen_phones: set[str] = set()
	for entry in members:
		name = (entry.get("display_name") or "").strip()
		if not name:
			continue
		phone = (entry.get("phone") or "").strip()
		if phone:
			common.validate_e164(phone)
			if phone in seen_phones:
				frappe.throw("The same phone was added twice.", frappe.ValidationError)
			seen_phones.add(phone)
		months = _claim(_as_months(entry.get("payout_months"), entry.get("payout_month")))
		clean_members.append({
			"display_name": name[:60],
			"urdu_name": (entry.get("urdu_name") or None),
			"phone": phone,
			"months": months,
		})

	start = getdate(start_month).replace(day=1)
	short_code = common.short_code_from(title)[:2]
	invite = common.new_invite_code()

	committee = frappe.new_doc("Kameti Committee")
	committee.title = title.strip()[:80]
	committee.urdu_title = (urdu_title or "").strip() or None
	committee.short_code = short_code
	committee.tone = tone if tone in common.AVATAR_TONES else "clay"
	committee.admin = user
	committee.members_count = members_count
	committee.installment_amount = installment_amount
	committee.start_month = start
	committee.duration_months = members_count
	committee.current_month = 0
	committee.cycle_state = "not_started"
	committee.invite_code = invite
	committee.insert(ignore_permissions=True)

	profile = frappe.db.get_value(
		"Kameti Profile", {"user": user},
		["display_name", "urdu_name", "phone", "avatar_tone"],
		as_dict=True,
	) or frappe._dict(
		display_name="Admin", urdu_name=None, phone="", avatar_tone="clay",
	)

	admin_mem = frappe.new_doc("Kameti Membership")
	admin_mem.kameti = committee.name
	admin_mem.user = user
	admin_mem.display_name = profile.display_name or "Admin"
	admin_mem.urdu_name = profile.urdu_name
	admin_mem.phone = profile.phone or ""
	admin_mem.initials = common.initials_from(profile.display_name or "Admin")
	admin_mem.avatar_tone = profile.avatar_tone or "clay"
	admin_mem.role = "admin"
	admin_mem.status = "active"
	admin_mem.payout_month = admin_months[0] if admin_months else None
	admin_mem.assignment_method = "manual" if admin_months else None
	admin_mem.joined_on = now_datetime()
	admin_mem.insert(ignore_permissions=True)

	# `committee.current_month` is 1 if before_save activated the committee
	# (start_month <= today), else 0. The matching slot starts as `current`.
	payout_amount = (members_count - 1) * installment_amount
	for i in range(1, members_count + 1):
		slot = frappe.new_doc("Payout Slot")
		slot.kameti = committee.name
		slot.month_index = i
		slot.month_date = add_months(start, i - 1)
		slot.payout_amount = payout_amount
		slot.status = "current" if i == committee.current_month else "upcoming"
		slot.insert(ignore_permissions=True)

	# --- create the added members + link every claimed slot ---------------
	for pm in admin_months:
		_link_slot(committee.name, pm, admin_mem.name)

	for m in clean_members:
		phone = m["phone"]
		months = m["months"]
		linked_user = common.user_name_for_phone(phone) if phone else None
		mem = frappe.new_doc("Kameti Membership")
		mem.kameti = committee.name
		mem.user = linked_user
		mem.display_name = m["display_name"]
		mem.urdu_name = m["urdu_name"]
		mem.phone = phone
		mem.initials = common.initials_from(m["display_name"])
		mem.avatar_tone = common.random_tone()
		mem.role = "member"
		mem.status = "active"
		mem.payout_month = months[0] if months else None
		mem.assignment_method = "manual" if months else None
		mem.joined_on = now_datetime()
		mem.insert(ignore_permissions=True)
		for pm in months:
			_link_slot(committee.name, pm, mem.name)

	for idx, acct in enumerate(payment_accounts or []):
		pa = frappe.new_doc("Payment Account")
		pa.kameti = committee.name
		pa.method = acct.get("method")
		pa.account_number = acct.get("account_number")
		pa.account_title = acct.get("account_title")
		pa.bank_name = acct.get("bank_name")
		pa.is_active = 1
		pa.display_order = idx
		pa.insert(ignore_permissions=True)

	frappe.db.commit()
	return {
		"id": committee.name,
		"invite_code": invite,
		"kameti": _committee_card(committee.name, user),
	}


@frappe.whitelist(methods=["GET"])
def get_committee(kameti: str):
	user = common.require_session_user()
	common.require_membership_or_admin(kameti, user)

	committee = frappe.get_doc("Kameti Committee", kameti)
	is_admin_caller = committee.admin == user

	admin_mem = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": kameti, "user": committee.admin, "role": "admin", "status": "active"},
		["name", "display_name", "initials", "avatar_tone", "phone"],
		as_dict=True,
	)

	payload = {
		"id": committee.name,
		"title": committee.title,
		"urdu_title": committee.urdu_title,
		"short_code": committee.short_code,
		"tone": committee.tone,
		"admin": {
			"membership_id": admin_mem.name if admin_mem else None,
			"display_name": admin_mem.display_name if admin_mem else None,
			"initials": admin_mem.initials if admin_mem else None,
			"tone": admin_mem.avatar_tone if admin_mem else None,
			"phone_masked": (
				common.mask_phone(admin_mem.phone) if admin_mem and admin_mem.phone else None
			),
		},
		"members_count": committee.members_count,
		"installment_amount": committee.installment_amount,
		"total_pool": (committee.installment_amount or 0) * (committee.members_count or 0),
		"start_month": (
			committee.start_month.isoformat() if committee.start_month else None
		),
		"current_month": committee.current_month,
		"cycle_state": committee.cycle_state,
		"members": _members_list(kameti, include_phone=is_admin_caller, caller=user),
		"roster": _roster_list(kameti),
		"payment_accounts": _payment_accounts_list(kameti),
	}
	if is_admin_caller:
		payload["invite_code"] = committee.invite_code
	return payload


@frappe.whitelist(methods=["POST"])
def update_committee(kameti: str, **kwargs):
	common.require_admin(kameti)
	committee = frappe.get_doc("Kameti Committee", kameti)
	editable = {"title", "urdu_title", "tone", "installment_amount",
				"start_month", "wa_template_id", "invite_expires_at"}
	if "members_count" in kwargs:
		if committee.cycle_state != "not_started":
			frappe.throw(
				"members_count cannot change after the cycle has started.",
				frappe.ValidationError,
			)
		editable.add("members_count")
	for k, v in kwargs.items():
		if k in editable:
			setattr(committee, k, v)
	committee.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def archive_committee(kameti: str):
	common.require_admin(kameti)
	frappe.db.set_value("Kameti Committee", kameti, "archived", 1)
	frappe.db.commit()
	return {"ok": True}


# ---- helpers --------------------------------------------------------

def _link_slot(kameti: str, month_index: int, membership_id: str):
	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index}, "name",
	)
	if slot:
		frappe.db.set_value("Payout Slot", slot, {
			"recipient": membership_id,
			"assignment_method": "manual",
			"assigned_on": now_datetime(),
		})


def _committee_card(name: str, user: str) -> dict:
	c = frappe.db.get_value(
		"Kameti Committee", name,
		["name", "title", "urdu_title", "short_code", "tone", "members_count",
		 "installment_amount", "current_month", "duration_months", "cycle_state",
		 "admin"],
		as_dict=True,
	) or {}
	role = "admin" if c.get("admin") == user else (
		frappe.db.get_value(
			"Kameti Membership",
			{"kameti": name, "user": user, "status": "active"},
			"role",
		) or "member"
	)
	c.pop("admin", None)
	c["id"] = c.pop("name", None)
	c["role"] = role
	return c


def _members_list(kameti: str, include_phone: bool, caller: str) -> list[dict]:
	rows = frappe.get_all(
		"Kameti Membership",
		filters={"kameti": kameti, "status": "active"},
		fields=["name", "display_name", "urdu_name", "initials", "avatar_tone",
				"role", "payout_month", "phone", "user", "joined_on"],
	)
	out = []
	for r in rows:
		out.append({
			"membership_id": r.name,
			"display_name": r.display_name,
			"urdu_name": r.urdu_name,
			"initials": r.initials,
			"tone": r.avatar_tone,
			"role": r.role,
			"payout_month": r.payout_month,
			"phone_masked": common.mask_phone(r.phone) if r.phone else None,
			"phone": r.phone if include_phone else None,
			"status": "active",
			"is_you": r.user == caller,
			"joined_on": r.joined_on.isoformat() if r.joined_on else None,
		})
	return out


def _roster_list(kameti: str) -> list[dict]:
	slots = frappe.get_all(
		"Payout Slot",
		filters={"kameti": kameti},
		fields=["month_index", "month_date", "recipient", "assignment_method",
				"assigned_on", "payout_amount", "status"],
		order_by="month_index asc",
	)
	out = []
	for s in slots:
		recip = None
		if s.recipient:
			m = frappe.db.get_value(
				"Kameti Membership", s.recipient,
				["display_name", "initials", "avatar_tone"], as_dict=True,
			)
			if m:
				recip = {
					"membership_id": s.recipient,
					"display_name": m.display_name,
					"initials": m.initials,
					"tone": m.avatar_tone,
				}
		out.append({
			"month_index": s.month_index,
			"month_date": s.month_date.isoformat() if s.month_date else None,
			"recipient": recip,
			"assignment_method": s.assignment_method,
			"assigned_on": s.assigned_on.isoformat() if s.assigned_on else None,
			"payout_amount": s.payout_amount,
			"status": s.status,
		})
	return out


def _payment_accounts_list(kameti: str) -> list[dict]:
	rows = frappe.get_all(
		"Payment Account",
		filters={"kameti": kameti, "is_active": 1},
		fields=["name", "method", "account_number", "account_title", "bank_name"],
		order_by="display_order asc",
	)
	for r in rows:
		r["id"] = r.pop("name", None)
	return rows
