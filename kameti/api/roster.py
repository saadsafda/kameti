"""Roster + lucky draw. See spec §8."""

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
	return {"slots": out}


@frappe.whitelist(methods=["POST"])
def assign_manual(kameti: str, month_index: int, membership_id: str):
	common.require_admin(kameti)
	month_index = int(month_index)

	slot_recipient = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index}, "recipient",
	)
	if slot_recipient and slot_recipient != membership_id:
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
		"Payout Slot",
		{"kameti": kameti, "month_index": month_index},
		{
			"recipient": membership_id,
			"assignment_method": "manual",
			"assigned_on": now_datetime(),
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

	slot_recipient = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index}, "recipient",
	)
	if slot_recipient:
		frappe.throw("That month is already assigned.", frappe.ValidationError)

	result = _do_draw(eligible)
	winner_id = result["winner_id"]

	frappe.db.set_value(
		"Payout Slot",
		{"kameti": kameti, "month_index": month_index},
		{
			"recipient": winner_id,
			"assignment_method": "lucky_draw",
			"assigned_on": now_datetime(),
			"draw_audit": frappe.as_json(result["audit"]),
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
	recipient = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index}, "recipient",
	)
	frappe.db.set_value(
		"Payout Slot",
		{"kameti": kameti, "month_index": month_index},
		{
			"recipient": None,
			"assignment_method": None,
			"assigned_on": None,
			"draw_audit": None,
		},
	)
	if recipient:
		frappe.db.set_value("Kameti Membership", recipient, {
			"payout_month": None,
			"assignment_method": None,
		})
	frappe.db.commit()
	return {"ok": True}


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
