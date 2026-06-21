"""MyKametisScreen hub endpoint. See spec §4.1."""

import frappe

from kameti.utils import common


@frappe.whitelist(methods=["GET"])
def get_my_kametis():
	user = common.require_session_user()

	memberships = frappe.get_all(
		"Kameti Membership",
		filters={"user": user, "status": "active"},
		fields=["name", "kameti", "role"],
	)

	result = []
	total_owed = 0
	owed_count = 0
	for m in memberships:
		c = frappe.db.get_value(
			"Kameti Committee", m.kameti,
			["name", "title", "urdu_title", "short_code", "tone", "members_count",
			 "installment_amount", "current_month", "duration_months", "cycle_state",
			 "archived"],
			as_dict=True,
		)
		if not c or c.archived:
			continue

		recipient = _current_recipient(m.kameti, c.current_month)
		counts = _payment_counts(m.kameti, c.current_month)
		last_payer = _last_payer(m.kameti, c.current_month)

		caller_paid = bool(frappe.db.exists(
			"Installment Payment",
			{"kameti": m.kameti, "payer": m.name, "payout_month": c.current_month,
			 "status": ("in", ("pending", "approved"))},
		)) if c.current_month else True
		caller_is_recipient = (
			bool(recipient) and recipient.get("membership_id") == m.name
		)
		if c.cycle_state == "active" and not caller_paid and not caller_is_recipient:
			total_owed += c.installment_amount or 0
			owed_count += 1

		result.append({
			"id": c.name,
			"title": c.title,
			"urdu_title": c.urdu_title,
			"short_code": c.short_code,
			"tone": c.tone,
			"role": m.role,
			"members_count": c.members_count,
			"installment_amount": c.installment_amount,
			"current_month": c.current_month,
			"duration_months": c.duration_months,
			"cycle_state": c.cycle_state,
			"this_month_recipient": recipient,
			"paid_count": counts["paid"],
			"pending_count": counts["pending"],
			"unpaid_count": counts["unpaid"],
			"last_payer": last_payer,
		})

	unread = frappe.db.count("Activity", {"recipient": user, "is_read": 0})
	return {
		"total_owed_this_month": total_owed,
		"owed_count": owed_count,
		"unread_notifications": unread,
		"kametis": result,
	}


def _current_recipient(kameti: str, month_index: int | None) -> dict | None:
	if not month_index:
		return None
	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month_index},
		["recipient", "payout_amount"], as_dict=True,
	)
	if not slot or not slot.recipient:
		return None
	m = frappe.db.get_value(
		"Kameti Membership", slot.recipient,
		["name", "display_name", "initials", "avatar_tone"], as_dict=True,
	)
	if not m:
		return None
	return {
		"membership_id": m.name,
		"display_name": m.display_name,
		"initials": m.initials,
		"tone": m.avatar_tone,
		"amount": slot.payout_amount,
	}


def _payment_counts(kameti: str, month_index: int | None) -> dict:
	active_members = frappe.db.count(
		"Kameti Membership", {"kameti": kameti, "status": "active"},
	)
	if not month_index:
		return {"paid": 0, "pending": 0, "unpaid": active_members}
	rows = frappe.db.sql(
		"""SELECT status, COUNT(*) AS c FROM `tabInstallment Payment`
		   WHERE kameti=%s AND payout_month=%s GROUP BY status""",
		(kameti, month_index), as_dict=True,
	)
	by = {r.status: r.c for r in rows}
	paid = by.get("approved", 0)
	pending = by.get("pending", 0)
	# minus 1 because the recipient doesn't pay themselves this month
	unpaid = max(active_members - 1 - paid - pending, 0)
	return {"paid": paid, "pending": pending, "unpaid": unpaid}


def _last_payer(kameti: str, month_index: int | None) -> dict | None:
	if not month_index:
		return None
	rows = frappe.db.sql(
		"""SELECT p.reviewed_on, m.display_name, m.initials, m.avatar_tone
		   FROM `tabInstallment Payment` p
		   JOIN `tabKameti Membership` m ON m.name = p.payer
		   WHERE p.kameti=%s AND p.payout_month=%s AND p.status='approved'
		   ORDER BY p.reviewed_on DESC LIMIT 1""",
		(kameti, month_index), as_dict=True,
	)
	if not rows:
		return None
	r = rows[0]
	return {
		"initials": r.initials,
		"tone": r.avatar_tone,
		"display_name": r.display_name,
		"paid_at": r.reviewed_on.isoformat() if r.reviewed_on else None,
	}
