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
	archived = []
	total_contributions = 0
	contribution_count = 0
	for m in memberships:
		c = frappe.db.get_value(
			"Kameti Committee", m.kameti,
			["name", "title", "urdu_title", "short_code", "tone", "members_count",
			 "installment_amount", "current_month", "duration_months", "cycle_state",
			 "archived"],
			as_dict=True,
		)
		if not c:
			continue
		# Archived kametis stay out of the main list, but the admin gets them
		# back in a separate bucket so archiving is reversible from the app.
		if c.archived:
			if m.role == "admin":
				archived.append({
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
				})
			continue

		recipient = _current_recipient(m.kameti, c.current_month)
		counts = _payment_counts(m.kameti, c.current_month)
		last_payer = _last_payer(m.kameti, c.current_month)

		# The hub card is a stable contributions summary, not an outstanding-
		# balance card. Include every current membership shown in the hub's
		# ACTIVE section, including committees at month 0 (not_started),
		# regardless of payment/recipient state. Only completed committees are
		# excluded.
		if c.cycle_state != "completed":
			total_contributions += c.installment_amount or 0
			contribution_count += 1

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
		"total_contributions_this_month": total_contributions,
		"contribution_count": contribution_count,
		# Backwards compatibility for app builds released before the summary
		# card was renamed from "owed" to "contributions".
		"total_owed_this_month": total_contributions,
		"owed_count": contribution_count,
		"unread_notifications": unread,
		"kametis": result,
		"archived_kametis": archived,
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
