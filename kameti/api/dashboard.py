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

	# Determine installment amount for this member (may be fractional for split slots)
	installment_amount = committee.installment_amount
	share_fraction = None
	if cm:
		slot_name = frappe.db.get_value(
			"Payout Slot", {"kameti": kameti, "month_index": cm}, "name",
		)
		if slot_name:
			share_fraction = frappe.db.get_value(
				"Slot Share", {"payout_slot": slot_name, "membership": mem.name}, "share_fraction",
			)
			if share_fraction:
				installment_amount = committee.installment_amount * share_fraction

	status_label = "upcoming"
	if cm:
		status_label = existing.status if existing else "unpaid"

	# Collect ALL payout slots this member is involved in:
	# 1. Solo slot (recipient field)
	# 2. Any split slots where they hold a Slot Share
	your_payouts = []

	solo_slot = frappe.db.get_value(
		"Payout Slot",
		{"kameti": kameti, "recipient": mem.name},
		["name", "month_index", "month_date", "payout_amount"], as_dict=True,
	)
	if solo_slot:
		your_payouts.append({
			"month_index": solo_slot.month_index,
			"month_label": (
				formatdate(solo_slot.month_date, "MMM yyyy")
				if solo_slot.month_date else None
			),
			"amount": solo_slot.payout_amount,
			"is_split": False,
			"share_fraction": None,
		})

	share_rows = frappe.get_all(
		"Slot Share",
		filters={"kameti": kameti, "membership": mem.name},
		fields=["payout_slot", "share_fraction"],
	)
	for sr in share_rows:
		slot_doc = frappe.db.get_value(
			"Payout Slot", sr.payout_slot,
			["month_index", "month_date", "payout_amount"], as_dict=True,
		)
		if slot_doc:
			your_payouts.append({
				"month_index": slot_doc.month_index,
				"month_label": (
					formatdate(slot_doc.month_date, "MMM yyyy")
					if slot_doc.month_date else None
				),
				"amount": slot_doc.payout_amount * sr.share_fraction,
				"is_split": True,
				"share_fraction": sr.share_fraction,
			})

	# Sort by month so the nearest payout comes first
	your_payouts.sort(key=lambda x: x["month_index"])

	# Also compute total monthly obligation for this member across ALL slots
	# (their own slot full amount + any co-holder fractions)
	# This is informational — actual payment validation happens per-month in pay.py.
	total_monthly_due = committee.installment_amount  # base: what all other members pay
	# If they're a co-holder in any slot that is active this month, their share
	# replaces/adds to the base. For display we show installment_amount for this month.
	# The installment_amount shown is what they owe THIS month (already computed above).

	return {
		"you": {
			"membership_id": mem.name,
			"display_name": mem.display_name,
			"initials": mem.initials,
			"tone": mem.avatar_tone,
		},
		"this_installment": {
			"amount": installment_amount,
			"share_fraction": share_fraction,
			"status": status_label,
			"due_on": due_on.isoformat() if due_on else None,
			"days_left": days_left,
			"existing_payment_id": existing.name if existing else None,
		},
		# your_payout kept for backwards compat — first/nearest payout
		"your_payout": your_payouts[0] if your_payouts else None,
		# full list of all slots this member benefits from
		"your_payouts": your_payouts,
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

	# Get recipient(s) for current month
	current_slot = None
	slot_is_split = False
	co_holder_ids: set[str] = set()
	if cm:
		current_slot = frappe.db.get_value(
			"Payout Slot", {"kameti": kameti, "month_index": cm},
			["name", "recipient", "is_split"], as_dict=True,
		)
		if current_slot and current_slot.is_split:
			slot_is_split = True
			co_holder_ids = set(frappe.get_all(
				"Slot Share",
				filters={"payout_slot": current_slot.name},
				pluck="membership",
			))

	recipient_id = (
		current_slot.recipient if current_slot and not slot_is_split else None
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

	# Build share fraction map for current month's split slot
	share_fractions: dict[str, float] = {}
	if slot_is_split and current_slot:
		for sh in frappe.get_all(
			"Slot Share",
			filters={"payout_slot": current_slot.name},
			fields=["membership", "share_fraction"],
		):
			share_fractions[sh.membership] = sh.share_fraction

	out_members = []
	collected = 0
	total_due = 0
	paid = 0
	unpaid_ids: list[str] = []
	non_recipient_count = 0

	for m in members:
		is_sole_recipient = (m.name == recipient_id)
		is_co_holder = m.name in co_holder_ids

		if is_sole_recipient:
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
		# Co-holders pay their fraction; regular members pay full amount
		frac = share_fractions.get(m.name, 1.0) if is_co_holder else 1.0
		amount = committee.installment_amount * frac
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

		row = {
			"membership_id": m.name,
			"display_name": m.display_name,
			"initials": m.initials,
			"tone": m.avatar_tone,
			"amount": amount,
			"status": status,
		}
		if is_co_holder:
			row["is_co_holder"] = True
			row["share_fraction"] = frac
		out_members.append(row)

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


def _recipient_card(kameti: str, cm: int) -> dict | list | None:
	month = cm or 1
	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month},
		["name", "recipient", "payout_amount", "is_split"], as_dict=True,
	)
	if not slot:
		return None

	if slot.is_split:
		shares = frappe.get_all(
			"Slot Share",
			filters={"payout_slot": slot.name},
			fields=["membership", "share_fraction"],
		)
		cards = []
		for sh in shares:
			m = frappe.db.get_value(
				"Kameti Membership", sh.membership,
				["display_name", "initials", "avatar_tone"], as_dict=True,
			)
			if m:
				cards.append({
					"membership_id": sh.membership,
					"display_name": m.display_name,
					"initials": m.initials,
					"tone": m.avatar_tone,
					"amount": slot.payout_amount * sh.share_fraction,
					"share_fraction": sh.share_fraction,
					"is_co_holder": True,
				})
		return cards if cards else None

	if not slot.recipient:
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
	from_month = cm or 1
	slots = frappe.get_all(
		"Payout Slot",
		filters={"kameti": kameti, "month_index": (">=", from_month)},
		fields=["name", "month_index", "recipient", "is_split"],
		order_by="month_index asc",
		limit=3,
	)
	out = []
	for s in slots:
		if s.is_split:
			# Show first two co-holders
			co_holders = frappe.get_all(
				"Slot Share",
				filters={"payout_slot": s.name},
				fields=["membership"],
				limit=2,
			)
			names = []
			for ch in co_holders:
				dn = frappe.db.get_value("Kameti Membership", ch.membership, "display_name")
				if dn:
					names.append(dn)
			display_name = " & ".join(names) if names else None
			item = {
				"month_index": s.month_index,
				"membership_id": None,
				"display_name": display_name,
				"is_split": True,
			}
		else:
			display_name = None
			if s.recipient:
				display_name = frappe.db.get_value(
					"Kameti Membership", s.recipient, "display_name",
				)
			item = {
				"month_index": s.month_index,
				"membership_id": s.recipient,
				"display_name": display_name,
				"is_split": False,
			}
		if cm and s.month_index == cm:
			item["label"] = "now"
		out.append(item)
	return out
