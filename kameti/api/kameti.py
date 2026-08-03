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
		# Drives the admin edit screen: once true, amount / start month /
		# size are frozen and the kameti can only be archived, not deleted.
		payload["edit_locked"] = settlement_started(kameti)
		payload["archived"] = bool(committee.archived)
	return payload


@frappe.whitelist(methods=["POST"])
def update_committee(kameti: str, **kwargs):
	"""Admin edit. Cosmetic fields are always editable; anything that changes
	the money or the schedule is frozen once the first payment is submitted.
	See `settlement_started`."""
	common.require_admin(kameti)
	committee = frappe.get_doc("Kameti Committee", kameti)

	# Always safe — these don't move money or shift the schedule.
	editable = {"title", "urdu_title", "tone", "wa_template_id",
				"invite_expires_at"}
	# Structural fields: allowed only while nobody has submitted a payment.
	structural = {"installment_amount", "start_month", "members_count"}

	requested_structural = structural & set(kwargs)
	if requested_structural and settlement_started(kameti):
		frappe.throw(
			"Payments have already been submitted for this kameti, so its "
			"amount, start month and size can no longer be changed.",
			frappe.ValidationError,
		)
	editable |= requested_structural

	old_members_count = committee.members_count
	old_start = committee.start_month
	old_amount = committee.installment_amount

	for k, v in kwargs.items():
		if k in editable:
			setattr(committee, k, v)

	new_members_count = int(committee.members_count or 0)
	if "members_count" in requested_structural:
		if not (4 <= new_members_count <= 30):
			frappe.throw("members_count must be between 4 and 30.", frappe.ValidationError)
		# Shrinking below the seats people already hold would orphan members.
		taken = frappe.db.count(
			"Kameti Membership", {"kameti": kameti, "status": "active"},
		)
		if new_members_count < taken:
			frappe.throw(
				f"This kameti already has {taken} members — reduce the roster "
				f"before lowering the size to {new_members_count}.",
				frappe.ValidationError,
			)
		# A slot beyond the new size can't survive the resize.
		orphan = frappe.db.sql(
			"""SELECT month_index FROM `tabPayout Slot`
			   WHERE kameti=%s AND month_index > %s AND recipient IS NOT NULL
			   ORDER BY month_index LIMIT 1""",
			(kameti, new_members_count),
		)
		if orphan:
			frappe.throw(
				f"Month {orphan[0][0]} is assigned to a member. Unassign it "
				f"before shrinking this kameti to {new_members_count} months.",
				frappe.ValidationError,
			)
		# duration always tracks size — one payout per member.
		committee.duration_months = new_members_count

	if "installment_amount" in requested_structural:
		if float(committee.installment_amount or 0) <= 0:
			frappe.throw("installment_amount must be > 0.", frappe.ValidationError)

	committee.save(ignore_permissions=True)

	# Keep the roster in step with the new configuration.
	changed_schedule = (
		("start_month" in requested_structural and getdate(old_start) != getdate(committee.start_month))
		or ("members_count" in requested_structural and int(old_members_count or 0) != new_members_count)
		or ("installment_amount" in requested_structural and float(old_amount or 0) != float(committee.installment_amount or 0))
	)
	if changed_schedule:
		_resync_slots(committee)

	frappe.db.commit()
	return {"ok": True, "edit_locked": settlement_started(kameti)}


@frappe.whitelist(methods=["POST"])
def archive_committee(kameti: str):
	common.require_admin(kameti)
	frappe.db.set_value("Kameti Committee", kameti, "archived", 1)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def unarchive_committee(kameti: str):
	"""Bring an archived kameti back into the hub."""
	common.require_admin(kameti)
	frappe.db.set_value("Kameti Committee", kameti, "archived", 0)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def delete_committee(kameti: str, confirm_title: str):
	"""Permanently delete a kameti and everything under it.

	Only possible before the first payment is submitted — once money is in
	play the kameti is a financial record and may only be archived. The admin
	must retype the exact title so a mis-tap can't wipe a group.
	"""
	common.require_admin(kameti)
	committee = frappe.get_doc("Kameti Committee", kameti)

	if settlement_started(kameti):
		frappe.throw(
			"Payments have already been submitted for this kameti, so it "
			"cannot be deleted. Archive it instead.",
			frappe.ValidationError,
		)

	if (confirm_title or "").strip() != (committee.title or "").strip():
		frappe.throw(
			"The name you typed doesn't match this kameti's name.",
			frappe.ValidationError,
		)

	# Tell everyone but the admin before the records disappear.
	admin_user = committee.admin
	member_users = frappe.get_all(
		"Kameti Membership",
		filters={"kameti": kameti, "status": "active"},
		pluck="user",
	)
	for u in {u for u in member_users if u and u != admin_user}:
		_notify_deleted(u, committee.title)

	# Children first, deepest link last, so no FK is left dangling.
	for doctype in (
		"Slot Share",
		"Installment Payment",
		"Payout Slot",
		"Payment Account",
		"Reminder",
		"Activity",
		"Kameti Membership",
	):
		for row in frappe.get_all(doctype, filters={"kameti": kameti}, pluck="name"):
			frappe.delete_doc(doctype, row, force=True, ignore_permissions=True,
							  delete_permanently=True)

	frappe.delete_doc("Kameti Committee", kameti, force=True,
					  ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	return {"ok": True, "deleted": kameti}


# ---- payment accounts -----------------------------------------------

VALID_ACCOUNT_METHODS = ("easypaisa", "jazzcash", "bank")


@frappe.whitelist(methods=["POST"])
def add_payment_account(
	kameti: str,
	method: str,
	account_number: str,
	account_title: str,
	bank_name: str | None = None,
):
	"""Add a collection account. Allowed at any time — members always need a
	current place to send money, even mid-cycle."""
	common.require_admin(kameti)
	if method not in VALID_ACCOUNT_METHODS:
		frappe.throw("Invalid payment method.", frappe.ValidationError)
	if not (account_number or "").strip():
		frappe.throw("Account number is required.", frappe.ValidationError)
	if not (account_title or "").strip():
		frappe.throw("Account title is required.", frappe.ValidationError)
	if method == "bank" and not (bank_name or "").strip():
		frappe.throw("Bank name is required for a bank account.", frappe.ValidationError)

	last = frappe.db.sql(
		"""SELECT MAX(display_order) FROM `tabPayment Account` WHERE kameti=%s""",
		(kameti,),
	)
	next_order = ((last[0][0] if last and last[0][0] is not None else -1) + 1)

	pa = frappe.new_doc("Payment Account")
	pa.kameti = kameti
	pa.method = method
	pa.account_number = account_number.strip()
	pa.account_title = account_title.strip()
	pa.bank_name = (bank_name or "").strip() or None
	pa.is_active = 1
	pa.display_order = next_order
	pa.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "id": pa.name}


@frappe.whitelist(methods=["POST"])
def update_payment_account(
	account_id: str,
	method: str | None = None,
	account_number: str | None = None,
	account_title: str | None = None,
	bank_name: str | None = None,
):
	pa = frappe.get_doc("Payment Account", account_id)
	common.require_admin(pa.kameti)

	if method is not None:
		if method not in VALID_ACCOUNT_METHODS:
			frappe.throw("Invalid payment method.", frappe.ValidationError)
		pa.method = method
	if account_number is not None:
		if not account_number.strip():
			frappe.throw("Account number is required.", frappe.ValidationError)
		pa.account_number = account_number.strip()
	if account_title is not None:
		if not account_title.strip():
			frappe.throw("Account title is required.", frappe.ValidationError)
		pa.account_title = account_title.strip()
	if bank_name is not None:
		pa.bank_name = bank_name.strip() or None
	if pa.method == "bank" and not (pa.bank_name or "").strip():
		frappe.throw("Bank name is required for a bank account.", frappe.ValidationError)

	pa.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def remove_payment_account(account_id: str):
	"""Deactivate rather than delete: past Installment Payments link to this
	account and must keep resolving."""
	pa = frappe.get_doc("Payment Account", account_id)
	common.require_admin(pa.kameti)
	frappe.db.set_value("Payment Account", account_id, "is_active", 0)
	frappe.db.commit()
	return {"ok": True}


# ---- helpers --------------------------------------------------------

def settlement_started(kameti: str) -> bool:
	"""True once ANY payment exists for this kameti — pending, approved or
	rejected.

	The lock deliberately fires the moment the first member submits a payment
	for approval, not when the admin approves it: from that point on members
	are treating the kameti as live, and changing the amount or the schedule
	under them would invalidate what they already paid against. Rejected rows
	still count — an unlock-on-rejection rule would be trivially abusable.
	"""
	return bool(frappe.db.exists("Installment Payment", {"kameti": kameti}))


def _resync_slots(committee) -> None:
	"""Rebuild the Payout Slot rows after a size / start-month / amount edit.

	Only reachable before any payment exists, so no approved money is ever
	re-dated. Assigned recipients are preserved; slots past the new size are
	dropped (the caller has already refused to shrink over an assigned slot).
	"""
	kameti = committee.name
	count = int(committee.members_count or 0)
	start = getdate(committee.start_month)
	payout_amount = (count - 1) * float(committee.installment_amount or 0)

	existing = {
		s.month_index: s
		for s in frappe.get_all(
			"Payout Slot", filters={"kameti": kameti},
			fields=["name", "month_index"],
		)
	}

	for i in range(1, count + 1):
		values = {
			"month_date": add_months(start, i - 1),
			"payout_amount": payout_amount,
			"status": "current" if i == committee.current_month else "upcoming",
		}
		if i in existing:
			frappe.db.set_value("Payout Slot", existing[i].name, values)
		else:
			slot = frappe.new_doc("Payout Slot")
			slot.kameti = kameti
			slot.month_index = i
			for k, v in values.items():
				setattr(slot, k, v)
			slot.insert(ignore_permissions=True)

	# Drop any slot that no longer fits the (smaller) kameti.
	for month_index, s in existing.items():
		if month_index > count:
			for share in frappe.get_all(
				"Slot Share", filters={"payout_slot": s.name}, pluck="name",
			):
				frappe.delete_doc("Slot Share", share, force=True,
								  ignore_permissions=True)
			frappe.delete_doc("Payout Slot", s.name, force=True,
							  ignore_permissions=True)


def _notify_deleted(user: str, title: str) -> None:
	"""In-app notice that a kameti the member belonged to was deleted. Written
	with no `kameti` link because the committee row is about to disappear."""
	a = frappe.new_doc("Activity")
	a.recipient = user
	a.type = "announcement"
	a.title = "Kameti deleted"
	a.body = f'"{title}" was deleted by its admin.'
	a.is_read = 0
	a.insert(ignore_permissions=True)


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
