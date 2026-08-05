"""Members management. See spec §7."""

import frappe
from frappe.utils import now_datetime

from kameti.utils import common


@frappe.whitelist(methods=["GET"])
def list(kameti: str, query: str | None = None):
	user = common.require_session_user()
	common.require_membership_or_admin(kameti, user)
	include_phone = common.is_admin(kameti, user)
	rows = frappe.get_all(
		"Kameti Membership",
		filters={"kameti": kameti, "status": "active"},
		fields=["name", "display_name", "urdu_name", "initials", "avatar_tone",
				"role", "payout_month", "phone", "user", "joined_on"],
	)
	q = (query or "").lower().strip()
	out = []
	for r in rows:
		if q and q not in (r.display_name or "").lower() and q not in (r.urdu_name or "").lower():
			continue
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
			"is_you": r.user == user,
			"joined_on": r.joined_on.isoformat() if r.joined_on else None,
			"this_month_status": _this_month_status(kameti, r.name),
		})
	return {"members": out}


@frappe.whitelist(methods=["POST"])
def add(
	kameti: str,
	display_name: str,
	phone: str | None = None,
	urdu_name: str | None = None,
	payout_month: int | None = None,
	avatar_tone: str | None = None,
):
	"""Add one member to an existing kameti.

	`phone` is optional, matching create_committee: a person added by name
	alone is a "ledger" member, and their membership auto-links when they
	later sign up with a matching phone.
	"""
	common.require_admin(kameti)
	phone = (phone or "").strip()
	if phone:
		common.validate_e164(phone)
	if not display_name or not display_name.strip():
		frappe.throw("Display name is required.", frappe.ValidationError)

	# Only a real phone can collide; ledger members share the empty string.
	if phone and frappe.db.exists(
		"Kameti Membership",
		{"kameti": kameti, "phone": phone, "status": "active"},
	):
		frappe.throw("This phone is already in the kameti.", frappe.ValidationError)

	if payout_month is not None:
		payout_month = int(payout_month)
		if frappe.db.exists(
			"Kameti Membership",
			{"kameti": kameti, "payout_month": payout_month, "status": "active"},
		):
			frappe.throw("That payout month is already taken.", frappe.ValidationError)

	existing_user = common.user_name_for_phone(phone) if phone else None

	# Re-adding someone previously removed revives their old membership, but
	# only a phone identifies them — ledger members all share an empty phone,
	# so match on one would revive an unrelated person.
	removed_name = (
		frappe.db.get_value(
			"Kameti Membership",
			{"kameti": kameti, "phone": phone, "status": "removed"},
			"name",
		)
		if phone
		else None
	)

	if removed_name:
		mem = frappe.get_doc("Kameti Membership", removed_name)
		mem.user = existing_user
		mem.display_name = display_name.strip()[:60]
		mem.urdu_name = urdu_name
		mem.initials = common.initials_from(display_name)
		mem.avatar_tone = (
			avatar_tone if avatar_tone in common.AVATAR_TONES else common.random_tone()
		)
		mem.role = "member"
		mem.status = "active"
		mem.payout_month = payout_month
		mem.assignment_method = "manual" if payout_month else None
		mem.joined_on = now_datetime()
		mem.removed_on = None
		mem.save(ignore_permissions=True)
	else:
		mem = frappe.new_doc("Kameti Membership")
		mem.kameti = kameti
		mem.user = existing_user
		mem.display_name = display_name.strip()[:60]
		mem.urdu_name = urdu_name
		mem.phone = phone
		mem.initials = common.initials_from(display_name)
		mem.avatar_tone = (
			avatar_tone if avatar_tone in common.AVATAR_TONES else common.random_tone()
		)
		mem.role = "member"
		mem.status = "active"
		mem.payout_month = payout_month
		mem.assignment_method = "manual" if payout_month else None
		mem.joined_on = now_datetime()
		mem.insert(ignore_permissions=True)

	if payout_month:
		_link_membership_to_slot(kameti, payout_month, mem.name)

	frappe.db.commit()
	return {
		"membership_id": mem.name,
		"is_existing_user": existing_user is not None,
		"invite_sent": False,
	}


@frappe.whitelist(methods=["POST"])
def update(
	membership_id: str,
	display_name: str | None = None,
	urdu_name: str | None = None,
	phone: str | None = None,
):
	"""Edit an existing member's name / urdu name / phone (admin only).

	Pass only the fields to change. Changing the phone re-validates it and
	rejects a number already used by another active member in the same kameti.
	`initials` are kept in sync with the display name.
	"""
	mem = frappe.get_doc("Kameti Membership", membership_id)
	common.require_admin(mem.kameti)

	if display_name is not None:
		name = display_name.strip()
		if not name:
			frappe.throw("Display name is required.", frappe.ValidationError)
		mem.display_name = name[:60]
		mem.initials = common.initials_from(name)

	if urdu_name is not None:
		mem.urdu_name = (urdu_name or "").strip() or None

	if phone is not None:
		phone = phone.strip()
		if phone:
			common.validate_e164(phone)
			dupe = frappe.db.exists(
				"Kameti Membership",
				{
					"kameti": mem.kameti,
					"phone": phone,
					"status": "active",
					"name": ["!=", mem.name],
				},
			)
			if dupe:
				frappe.throw(
					"This phone is already in the kameti.", frappe.ValidationError
				)
			# Re-link to a signed-up user if this phone matches one.
			mem.user = common.user_name_for_phone(phone) or mem.user
		mem.phone = phone

	mem.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def remove(membership_id: str, reason: str | None = None):
	mem = frappe.get_doc("Kameti Membership", membership_id)
	common.require_admin(mem.kameti)
	if mem.role == "admin":
		frappe.throw(
			"Cannot remove the admin. Transfer ownership first.",
			frappe.ValidationError,
		)
	mem.status = "removed"
	mem.removed_on = now_datetime()
	mem.save(ignore_permissions=True)

	if mem.payout_month:
		slot = frappe.db.get_value(
			"Payout Slot",
			{"kameti": mem.kameti, "month_index": mem.payout_month},
			["name", "is_split"], as_dict=True,
		)
		if slot:
			if slot.is_split:
				# Delegate to roster helper to handle reversion-to-single logic
				from kameti.api.roster import remove_slot_share as _remove_share
				_remove_share(
					kameti=mem.kameti,
					month_index=mem.payout_month,
					membership_id=mem.name,
				)
			else:
				frappe.db.set_value("Payout Slot", slot.name, {
					"recipient": None,
					"assignment_method": None,
					"assigned_on": None,
				})
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def assign_payout_month(membership_id: str, payout_month: int):
	mem = frappe.get_doc("Kameti Membership", membership_id)
	common.require_admin(mem.kameti)
	payout_month = int(payout_month)

	target_slot = frappe.db.get_value(
		"Payout Slot",
		{"kameti": mem.kameti, "month_index": payout_month},
		["name", "recipient", "is_split"], as_dict=True,
	)
	if target_slot and target_slot.is_split:
		frappe.throw(
			"That slot is split between multiple members. Use split_slot to manage co-holders.",
			frappe.ValidationError,
		)
	if frappe.db.exists(
		"Kameti Membership",
		{"kameti": mem.kameti, "payout_month": payout_month, "status": "active",
		 "name": ("!=", mem.name)},
	):
		frappe.throw("That month is already assigned.", frappe.ValidationError)

	if mem.payout_month and mem.payout_month != payout_month:
		old_slot = frappe.db.get_value(
			"Payout Slot",
			{"kameti": mem.kameti, "month_index": mem.payout_month},
			"name",
		)
		if old_slot:
			frappe.db.set_value("Payout Slot", old_slot, {
				"recipient": None,
				"assignment_method": None,
				"assigned_on": None,
			})
	mem.payout_month = payout_month
	mem.assignment_method = "manual"
	mem.save(ignore_permissions=True)
	_link_membership_to_slot(mem.kameti, payout_month, mem.name)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def transfer_ownership(kameti: str, new_admin_membership_id: str):
	common.require_admin(kameti)
	new_admin = frappe.get_doc("Kameti Membership", new_admin_membership_id)
	if new_admin.kameti != kameti or new_admin.status != "active" or not new_admin.user:
		frappe.throw(
			"The new admin must be an active app-linked member.",
			frappe.ValidationError,
		)

	committee = frappe.get_doc("Kameti Committee", kameti)
	old_user = committee.admin
	committee.admin = new_admin.user
	committee.save(ignore_permissions=True)

	new_admin.role = "admin"
	new_admin.save(ignore_permissions=True)

	old_mem = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": kameti, "user": old_user, "role": "admin", "status": "active"},
		"name",
	)
	if old_mem:
		frappe.db.set_value("Kameti Membership", old_mem, "role", "member")
	frappe.db.commit()
	return {"ok": True}


# ---- helpers ------------------------------------------------------

def _link_membership_to_slot(kameti: str, month_index: int, membership_id: str):
	slot = frappe.db.get_value(
		"Payout Slot",
		{"kameti": kameti, "month_index": month_index},
		"name",
	)
	if slot:
		frappe.db.set_value("Payout Slot", slot, {
			"recipient": membership_id,
			"assignment_method": "manual",
			"assigned_on": now_datetime(),
		})


def _this_month_status(kameti: str, membership_id: str) -> str:
	cm = frappe.db.get_value("Kameti Committee", kameti, "current_month")
	if not cm:
		return "unpaid"
	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": cm},
		["name", "recipient", "is_split"], as_dict=True,
	)
	if slot:
		if not slot.is_split and slot.recipient == membership_id:
			return "receiver"
		if slot.is_split:
			is_co_holder = frappe.db.exists(
				"Slot Share", {"payout_slot": slot.name, "membership": membership_id},
			)
			if is_co_holder:
				# Co-holders are receivers AND still need to pay their share;
				# show payment status rather than "receiver" so they know to pay.
				pass
	pay = frappe.db.get_value(
		"Installment Payment",
		{"kameti": kameti, "payer": membership_id, "payout_month": cm,
		 "status": ("in", ("pending", "approved"))},
		"status",
	)
	if pay == "approved":
		return "paid"
	if pay == "pending":
		return "pending"
	return "unpaid"
