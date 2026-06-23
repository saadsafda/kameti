"""Roster + lucky draw + slot splitting. See spec §8."""

import frappe
from frappe.utils import now_datetime

from kameti.utils import common
from kameti.utils.lucky_draw import draw as _do_draw


@frappe.whitelist(methods=["GET"])
def get(kameti: str):
	common.require_membership_or_admin(kameti)
	slots = frappe.get_all(
		"Payout Slot",
		filters={"kameti": kameti},
		fields=["name", "month_index", "month_date", "recipient", "assignment_method",
				"assigned_on", "payout_amount", "is_split", "status"],
		order_by="month_index asc",
	)
	out = []
	for s in slots:
		recip = None
		if s.recipient and not s.is_split:
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

		slot_shares = []
		if s.is_split:
			shares = frappe.get_all(
				"Slot Share",
				filters={"payout_slot": s.name},
				fields=["name", "membership", "share_fraction"],
				order_by="creation asc",
			)
			for sh in shares:
				m = frappe.db.get_value(
					"Kameti Membership", sh.membership,
					["display_name", "initials", "avatar_tone"], as_dict=True,
				)
				if m:
					slot_shares.append({
						"share_id": sh.name,
						"membership_id": sh.membership,
						"display_name": m.display_name,
						"initials": m.initials,
						"tone": m.avatar_tone,
						"share_fraction": sh.share_fraction,
					})

		out.append({
			"slot_id": s.name,
			"month_index": s.month_index,
			"month_date": s.month_date.isoformat() if s.month_date else None,
			"recipient": recip,
			"is_split": bool(s.is_split),
			"slot_shares": slot_shares,
			"assignment_method": s.assignment_method,
			"assigned_on": s.assigned_on.isoformat() if s.assigned_on else None,
			"payout_amount": s.payout_amount,
			"status": s.status,
		})
	return {"slots": out}


@frappe.whitelist(methods=["POST"])
def assign_manual(kameti: str, month_index: int, membership_id: str):
	common.require_admin(kameti)
	month_index = int(month_index)

	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index},
		["name", "recipient", "is_split"], as_dict=True,
	)
	if not slot:
		frappe.throw("Slot not found.", frappe.ValidationError)

	if slot.is_split:
		frappe.throw(
			"This slot is already split between multiple members. "
			"Remove the split first or use split_slot to add co-holders.",
			frappe.ValidationError,
		)

	if slot.recipient and slot.recipient != membership_id:
		frappe.throw("That month is already assigned.", frappe.ValidationError)

	mem = frappe.db.get_value(
		"Kameti Membership", membership_id,
		["name", "kameti", "status", "payout_month"], as_dict=True,
	)
	if not mem or mem.kameti != kameti or mem.status != "active":
		frappe.throw("Invalid membership.", frappe.ValidationError)

	if mem.payout_month and mem.payout_month != month_index:
		old_slot = frappe.db.get_value(
			"Payout Slot",
			{"kameti": kameti, "month_index": mem.payout_month},
			"name",
		)
		if old_slot:
			frappe.db.set_value("Payout Slot", old_slot, {
				"recipient": None,
				"assignment_method": None,
				"assigned_on": None,
			})

	frappe.db.set_value(
		"Payout Slot", slot.name,
		{
			"recipient": membership_id,
			"assignment_method": "manual",
			"assigned_on": now_datetime(),
			"is_split": 0,
		},
	)
	frappe.db.set_value(
		"Kameti Membership", membership_id,
		{"payout_month": month_index, "assignment_method": "manual"},
	)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def lucky_draw(kameti: str, month_index: int):
	common.require_admin(kameti)
	month_index = int(month_index)

	eligible = frappe.get_all(
		"Kameti Membership",
		filters={"kameti": kameti, "status": "active",
				 "payout_month": ("is", "not set")},
		pluck="name",
	)
	if not eligible:
		frappe.throw("No eligible members for the draw.", frappe.ValidationError)

	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index},
		["name", "recipient", "is_split"], as_dict=True,
	)
	if slot and (slot.recipient or slot.is_split):
		frappe.throw("That month is already assigned.", frappe.ValidationError)

	result = _do_draw(eligible)
	winner_id = result["winner_id"]

	frappe.db.set_value(
		"Payout Slot", slot.name,
		{
			"recipient": winner_id,
			"assignment_method": "lucky_draw",
			"assigned_on": now_datetime(),
			"draw_audit": frappe.as_json(result["audit"]),
			"is_split": 0,
		},
	)
	frappe.db.set_value(
		"Kameti Membership", winner_id,
		{"payout_month": month_index, "assignment_method": "lucky_draw"},
	)

	winner = frappe.db.get_value(
		"Kameti Membership", winner_id,
		["display_name", "initials", "avatar_tone", "user"], as_dict=True,
	)
	if winner.user:
		_activity(
			recipient=winner.user, kameti=kameti, type_="draw_complete",
			title="You won the draw",
			body=f"You'll receive the payout in month {month_index}.",
			payload={"month_index": month_index},
		)
	frappe.db.commit()
	return {
		"winner": {
			"membership_id": winner_id,
			"display_name": winner.display_name,
			"initials": winner.initials,
			"tone": winner.avatar_tone,
		},
		"audit": result["audit"],
	}


@frappe.whitelist(methods=["POST"])
def unassign(kameti: str, month_index: int):
	common.require_admin(kameti)
	month_index = int(month_index)

	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index},
		["name", "recipient", "is_split"], as_dict=True,
	)
	if not slot:
		return {"ok": True}

	# Clear split shares first
	if slot.is_split:
		shares = frappe.get_all(
			"Slot Share", filters={"payout_slot": slot.name},
			fields=["name", "membership"],
		)
		for sh in shares:
			existing_month = frappe.db.get_value(
				"Kameti Membership", sh.membership, "payout_month",
			)
			if existing_month == month_index:
				frappe.db.set_value("Kameti Membership", sh.membership, {
					"payout_month": None, "assignment_method": None,
				})
			frappe.delete_doc("Slot Share", sh.name, ignore_permissions=True)

	frappe.db.set_value(
		"Payout Slot", slot.name,
		{
			"recipient": None,
			"assignment_method": None,
			"assigned_on": None,
			"draw_audit": None,
			"is_split": 0,
		},
	)
	if slot.recipient and not slot.is_split:
		frappe.db.set_value("Kameti Membership", slot.recipient, {
			"payout_month": None,
			"assignment_method": None,
		})
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def split_slot(kameti: str, month_index: int, membership_ids: list, share_fractions: list):
	"""
	Assign multiple members to one slot with fractional shares.
	membership_ids and share_fractions must be equal-length lists.
	Share fractions must sum to 1.0.

	Converts the slot from single-recipient to split mode.
	Existing single recipient is removed first.
	"""
	common.require_admin(kameti)
	month_index = int(month_index)

	if isinstance(membership_ids, str):
		membership_ids = frappe.parse_json(membership_ids)
	if isinstance(share_fractions, str):
		share_fractions = frappe.parse_json(share_fractions)

	if len(membership_ids) < 2:
		frappe.throw("Split requires at least 2 members.", frappe.ValidationError)
	if len(membership_ids) != len(share_fractions):
		frappe.throw("membership_ids and share_fractions must have the same length.", frappe.ValidationError)

	share_fractions = [float(f) for f in share_fractions]
	total = sum(share_fractions)
	if abs(total - 1.0) > 0.001:
		frappe.throw(f"Share fractions must sum to 1.0 (got {total:.3f}).", frappe.ValidationError)

	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index},
		["name", "recipient", "is_split", "payout_amount"], as_dict=True,
	)
	if not slot:
		frappe.throw("Slot not found.", frappe.ValidationError)

	# Validate all members belong to this kameti and are active; duplicates not allowed
	seen = set()
	for mid in membership_ids:
		if mid in seen:
			frappe.throw(f"Duplicate member in list: {mid}", frappe.ValidationError)
		seen.add(mid)
		mem = frappe.db.get_value(
			"Kameti Membership", mid, ["kameti", "status"], as_dict=True,
		)
		if not mem or mem.kameti != kameti or mem.status != "active":
			frappe.throw(f"Invalid membership: {mid}", frappe.ValidationError)
		# A member may already own a different solo slot — that is fine.
		# They participate here as a co-holder; payout_month is NOT changed for them
		# (their existing solo-slot ownership is preserved).

	# Clear previous state (single recipient or old split)
	_clear_slot(slot)

	now = now_datetime()

	# Create share records.
	# Only update payout_month when the member doesn't already own a different slot
	# (they participate as co-holder via Slot Share regardless).
	for mid, frac in zip(membership_ids, share_fractions):
		sh = frappe.new_doc("Slot Share")
		sh.payout_slot = slot.name
		sh.kameti = kameti
		sh.membership = mid
		sh.share_fraction = frac
		sh.assigned_on = now
		sh.insert(ignore_permissions=True)

		existing_month = frappe.db.get_value("Kameti Membership", mid, "payout_month")
		if not existing_month or existing_month == month_index:
			frappe.db.set_value("Kameti Membership", mid, {
				"payout_month": month_index,
				"assignment_method": "manual",
			})

	frappe.db.set_value(
		"Payout Slot", slot.name,
		{
			"recipient": None,
			"assignment_method": "manual",
			"assigned_on": now,
			"is_split": 1,
		},
	)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def update_slot_share(kameti: str, month_index: int, membership_id: str, share_fraction: float):
	"""Update a single co-holder's share fraction. Re-validates that all fractions still sum to 1."""
	common.require_admin(kameti)
	month_index = int(month_index)
	share_fraction = float(share_fraction)

	slot_name = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index}, "name",
	)
	if not slot_name:
		frappe.throw("Slot not found.", frappe.ValidationError)

	share = frappe.db.get_value(
		"Slot Share", {"payout_slot": slot_name, "membership": membership_id}, "name",
	)
	if not share:
		frappe.throw("Share record not found for this member.", frappe.ValidationError)

	all_shares = frappe.get_all(
		"Slot Share",
		filters={"payout_slot": slot_name},
		fields=["name", "membership", "share_fraction"],
	)
	total = sum(
		share_fraction if s.membership == membership_id else s.share_fraction
		for s in all_shares
	)
	if abs(total - 1.0) > 0.001:
		frappe.throw(
			f"Updated fractions would sum to {total:.3f}, not 1.0.",
			frappe.ValidationError,
		)

	frappe.db.set_value("Slot Share", share, "share_fraction", share_fraction)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def remove_slot_share(kameti: str, month_index: int, membership_id: str):
	"""
	Remove one co-holder from a split slot.
	If only one holder remains after removal, the slot reverts to single-recipient mode.
	"""
	common.require_admin(kameti)
	month_index = int(month_index)

	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index},
		["name", "is_split"], as_dict=True,
	)
	if not slot or not slot.is_split:
		frappe.throw("Slot is not in split mode.", frappe.ValidationError)

	share = frappe.db.get_value(
		"Slot Share", {"payout_slot": slot.name, "membership": membership_id}, "name",
	)
	if not share:
		frappe.throw("Share not found for this member.", frappe.ValidationError)

	frappe.delete_doc("Slot Share", share, ignore_permissions=True)
	# Only clear payout_month when this co-holder's primary slot IS this month
	# (i.e. they don't own a different solo slot).
	existing_month = frappe.db.get_value("Kameti Membership", membership_id, "payout_month")
	if existing_month == month_index:
		frappe.db.set_value("Kameti Membership", membership_id, {
			"payout_month": None, "assignment_method": None,
		})

	remaining = frappe.get_all(
		"Slot Share", filters={"payout_slot": slot.name},
		fields=["name", "membership", "share_fraction"],
	)

	if len(remaining) == 1:
		# Revert to single-recipient mode
		sole = remaining[0]
		frappe.db.set_value(
			"Payout Slot", slot.name,
			{
				"recipient": sole.membership,
				"is_split": 0,
				"assignment_method": "manual",
				"assigned_on": now_datetime(),
			},
		)
		frappe.delete_doc("Slot Share", sole.name, ignore_permissions=True)
	elif len(remaining) == 0:
		frappe.db.set_value(
			"Payout Slot", slot.name,
			{
				"recipient": None,
				"is_split": 0,
				"assignment_method": None,
				"assigned_on": None,
			},
		)

	frappe.db.commit()
	return {"ok": True}


# ---- helpers ---------------------------------------------------------------

def _clear_slot(slot):
	"""Remove all existing assignments for a slot (single or split).
	For co-holders whose primary payout_month is a *different* slot, their
	payout_month is left intact — only the Slot Share record is removed.
	"""
	if slot.is_split:
		shares = frappe.get_all(
			"Slot Share", filters={"payout_slot": slot.name},
			fields=["name", "membership"],
		)
		month_index = frappe.db.get_value("Payout Slot", slot.name, "month_index")
		for sh in shares:
			existing_month = frappe.db.get_value(
				"Kameti Membership", sh.membership, "payout_month",
			)
			if existing_month == month_index:
				frappe.db.set_value("Kameti Membership", sh.membership, {
					"payout_month": None, "assignment_method": None,
				})
			frappe.delete_doc("Slot Share", sh.name, ignore_permissions=True)
	elif slot.recipient:
		frappe.db.set_value("Kameti Membership", slot.recipient, {
			"payout_month": None, "assignment_method": None,
		})


def get_member_share_for_slot(slot_name: str, membership_id: str) -> float | None:
	"""Return this member's share fraction for a split slot, or None if not a co-holder."""
	return frappe.db.get_value(
		"Slot Share",
		{"payout_slot": slot_name, "membership": membership_id},
		"share_fraction",
	)


def get_slot_co_holder_ids(slot_name: str) -> list[str]:
	"""Return membership IDs of all co-holders of a split slot."""
	return frappe.get_all(
		"Slot Share", filters={"payout_slot": slot_name}, pluck="membership",
	)


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
