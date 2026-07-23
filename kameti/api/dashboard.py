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
		# Include 'rejected' so the member's dashboard still gets an
		# existing_payment_id to look up — that's how the client surfaces
		# the rejection (reason, resubmit CTA) instead of silently
		# reverting to a plain "you owe" card. status_label below still
		# reports 'unpaid' for a rejected receipt since the member owes
		# again either way. A resubmit after rejection leaves both rows
		# in place, so prefer the active (pending/approved) one over a
		# stale rejected one, then fall back to the most recent.
		rows = frappe.db.sql(
			"""SELECT name, status FROM `tabInstallment Payment`
			   WHERE kameti=%s AND payer=%s AND payout_month=%s
			     AND status IN ('pending', 'approved', 'rejected')
			   ORDER BY FIELD(status, 'pending', 'approved', 'rejected'), submitted_on DESC
			   LIMIT 1""",
			(kameti, mem.name, cm), as_dict=True,
		)
		existing = rows[0] if rows else None

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
		status_label = existing.status if existing and existing.status != "rejected" else "unpaid"

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
		"organizer": _organizer(committee),
		"payment_history": _payment_history(committee, mem.name),
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


def _organizer(committee) -> dict | None:
	"""The committee admin, resolved to their membership card for display."""
	if not committee.admin:
		return None
	mem = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": committee.name, "user": committee.admin},
		["display_name", "initials", "avatar_tone"],
		as_dict=True,
	)
	if mem:
		return {
			"display_name": mem.display_name,
			"initials": mem.initials,
			"tone": mem.avatar_tone,
		}
	# Admin isn't a member (or membership missing) — fall back to the user's
	# full name so the card still shows a real person.
	full_name = frappe.db.get_value("User", committee.admin, "full_name")
	if not full_name:
		return None
	return {"display_name": full_name, "initials": None, "tone": None}


def _payment_history(committee, membership: str) -> list[dict]:
	"""One row per elapsed month for this member: whether they paid, and when.

	Covers months 1..current_month. For each, we look up the member's own
	installment payment (the slot they pay INTO for that month) and derive a
	status the client can render directly:
	  - paid     : an approved payment exists (on-time / late by N days vs due)
	  - pending  : a receipt is submitted but not yet approved
	  - due      : the current month, still unpaid
	  - upcoming : month hasn't come due yet (only if included)
	"""
	cm = committee.current_month or 0
	if not cm:
		return []

	# Pull every payment this member made in this kameti once, keyed by month.
	rows = frappe.get_all(
		"Installment Payment",
		filters={"kameti": committee.name, "payer": membership},
		fields=["payout_month", "status", "submitted_on", "reviewed_on"],
		order_by="submitted_on asc",
	)
	# Prefer approved > pending > rejected when multiple rows exist for a month.
	rank = {"approved": 0, "pending": 1, "rejected": 2}
	by_month: dict[int, dict] = {}
	for r in rows:
		m = r.payout_month
		if m not in by_month or rank.get(r.status, 9) < rank.get(by_month[m].status, 9):
			by_month[m] = r

	out = []
	for m in range(1, cm + 1):
		due = _due_date(committee, m)
		month_date = add_months(committee.start_month, m - 1) if committee.start_month else None
		row = by_month.get(m)
		status = "due" if m == cm else "unpaid"
		paid_on = None
		days_late = 0
		if row:
			if row.status == "approved":
				status = "paid"
				paid_on = row.reviewed_on or row.submitted_on
				# Lateness measured against the due date using the submission day.
				if due and row.submitted_on:
					diff = date_diff(getdate(row.submitted_on), due)
					days_late = diff if diff > 0 else 0
			elif row.status == "pending":
				status = "pending"
				paid_on = row.submitted_on
		out.append({
			"month_index": m,
			"month_label": formatdate(month_date, "MMM") if month_date else None,
			"month_date": month_date.isoformat() if month_date else None,
			"due_on": due.isoformat() if due else None,
			"status": status,
			"paid_on": paid_on.isoformat() if paid_on else None,
			"days_late": days_late,
		})
	return out


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
