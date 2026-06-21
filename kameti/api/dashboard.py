"""Dashboard payloads. See spec §9."""

import frappe
from frappe.utils import add_months, date_diff, formatdate, getdate, today

from kameti.utils import common


@frappe.whitelist(methods=["GET"])
def get_admin_dashboard(kameti: str):
	common.require_admin(kameti)
	committee = frappe.get_doc("Kameti Committee", kameti)
	return _build_admin(committee)


@frappe.whitelist(methods=["GET"])
def get_member_dashboard(kameti: str):
	mem = common.require_membership(kameti)
	committee = frappe.get_doc("Kameti Committee", kameti)
	cm = committee.current_month or 0

	existing = None
	if cm:
		existing = frappe.db.get_value(
			"Installment Payment",
			{"kameti": kameti, "payer": mem.name, "payout_month": cm,
			 "status": ("in", ("pending", "approved"))},
			["name", "status"], as_dict=True,
		)

	due_on = _due_date(committee, cm)
	days_left = date_diff(due_on, today()) if due_on else None
	status_label = "upcoming"
	if cm:
		status_label = existing.status if existing else "unpaid"

	your_slot = frappe.db.get_value(
		"Payout Slot",
		{"kameti": kameti, "recipient": mem.name},
		["month_index", "month_date", "payout_amount"], as_dict=True,
	)

	return {
		"you": {
			"membership_id": mem.name,
			"display_name": mem.display_name,
			"initials": mem.initials,
			"tone": mem.avatar_tone,
		},
		"this_installment": {
			"amount": committee.installment_amount,
			"status": status_label,
			"due_on": due_on.isoformat() if due_on else None,
			"days_left": days_left,
			"existing_payment_id": existing.name if existing else None,
		},
		"your_payout": (
			{
				"month_index": your_slot.month_index,
				"month_label": (
					formatdate(your_slot.month_date, "MMM yyyy")
					if your_slot.month_date else None
				),
				"amount": your_slot.payout_amount,
			} if your_slot else None
		),
		"progress": {"current": cm, "total": committee.members_count},
		"this_month_recipient": _recipient_card(kameti, cm),
		"schedule_preview": _schedule_preview(kameti, cm),
	}


# ---- helpers ------------------------------------------------------

def _build_admin(committee) -> dict:
	kameti = committee.name
	cm = committee.current_month or 0
	members = frappe.get_all(
		"Kameti Membership",
		filters={"kameti": kameti, "status": "active"},
		fields=["name", "display_name", "initials", "avatar_tone", "user"],
	)
	recipient_id = (
		frappe.db.get_value(
			"Payout Slot", {"kameti": kameti, "month_index": cm}, "recipient",
		) if cm else None
	)

	latest_status: dict[str, str] = {}
	if cm:
		rows = frappe.db.sql(
			"""SELECT payer, status FROM `tabInstallment Payment`
			   WHERE kameti=%s AND payout_month=%s
			   ORDER BY submitted_on DESC""",
			(kameti, cm), as_dict=True,
		)
		for r in rows:
			latest_status.setdefault(r.payer, r.status)

	out_members = []
	collected = 0
	total_due = 0
	paid = 0
	unpaid_ids: list[str] = []
	non_recipient_count = 0

	for m in members:
		if m.name == recipient_id:
			out_members.append({
				"membership_id": m.name,
				"display_name": m.display_name,
				"initials": m.initials,
				"tone": m.avatar_tone,
				"amount": (committee.members_count - 1) * committee.installment_amount,
				"status": "receiver",
			})
			continue
		non_recipient_count += 1
		amount = committee.installment_amount
		total_due += amount
		raw = latest_status.get(m.name)
		if raw == "approved":
			status = "paid"
			collected += amount
			paid += 1
		elif raw == "pending":
			status = "pending"
		else:
			status = "unpaid"
			unpaid_ids.append(m.name)
		out_members.append({
			"membership_id": m.name,
			"display_name": m.display_name,
			"initials": m.initials,
			"tone": m.avatar_tone,
			"amount": amount,
			"status": status,
		})

	due_on = _due_date(committee, cm)
	cm_label = None
	if cm and committee.start_month:
		cm_label = formatdate(add_months(committee.start_month, cm - 1), "MMM yyyy")

	pending_approvals = frappe.db.count(
		"Installment Payment", {"kameti": kameti, "status": "pending"},
	)

	return {
		"cycle_state": committee.cycle_state,
		"current_month": cm,
		"current_month_label": cm_label,
		"due_on": due_on.isoformat() if due_on else None,
		"this_month_recipient": _recipient_card(kameti, cm),
		"collected": collected,
		"total_due": total_due,
		"paid_of": f"{paid}/{non_recipient_count}",
		"members": out_members,
		"unpaid_member_ids": unpaid_ids,
		"pending_approvals_count": pending_approvals,
	}


def _recipient_card(kameti: str, cm: int) -> dict | None:
	# Before the cycle starts (current_month == 0) preview month 1's recipient
	# so a freshly-created kameti still shows its real roster.
	month = cm or 1
	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month},
		["recipient", "payout_amount"], as_dict=True,
	)
	if not slot or not slot.recipient:
		return None
	m = frappe.db.get_value(
		"Kameti Membership", slot.recipient,
		["display_name", "initials", "avatar_tone"], as_dict=True,
	)
	if not m:
		return None
	return {
		"membership_id": slot.recipient,
		"display_name": m.display_name,
		"initials": m.initials,
		"tone": m.avatar_tone,
		"amount": slot.payout_amount,
	}


def _due_date(committee, cm: int):
	if not cm or not committee.start_month:
		return None
	month_start = add_months(committee.start_month, cm - 1)
	return getdate(month_start).replace(day=5)


def _schedule_preview(kameti: str, cm: int) -> list[dict]:
	# Before the cycle starts (current_month == 0) preview from month 1 so a
	# freshly-created kameti still shows its real upcoming schedule.
	from_month = cm or 1
	slots = frappe.get_all(
		"Payout Slot",
		filters={"kameti": kameti, "month_index": (">=", from_month)},
		fields=["month_index", "recipient"],
		order_by="month_index asc",
		limit=3,
	)
	out = []
	for s in slots:
		display_name = None
		if s.recipient:
			display_name = frappe.db.get_value(
				"Kameti Membership", s.recipient, "display_name",
			)
		item = {
			"month_index": s.month_index,
			"membership_id": s.recipient,
			"display_name": display_name,
		}
		# Only flag a row as "now" once the cycle is actually running.
		if cm and s.month_index == cm:
			item["label"] = "now"
		out.append(item)
	return out
