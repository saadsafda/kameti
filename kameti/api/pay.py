"""Pay flow + admin approval queue. See spec §10."""

import frappe
from frappe.utils import now_datetime, pretty_date, time_diff_in_seconds

from kameti.utils import common
from kameti.api.roster import get_member_share_for_slot


VALID_METHODS = ("easypaisa", "jazzcash", "bank", "cash", "other")


@frappe.whitelist(methods=["GET"])
def get_payment_methods(kameti: str):
	common.require_membership_or_admin(kameti)
	committee_title = frappe.db.get_value("Kameti Committee", kameti, "title")
	admin_user = frappe.db.get_value("Kameti Committee", kameti, "admin")
	admin_name = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": kameti, "user": admin_user, "role": "admin", "status": "active"},
		"display_name",
	) or "Admin"
	admin_phone = frappe.db.get_value(
		"Kameti Profile", {"user": admin_user}, "phone",
	)
	rows = frappe.get_all(
		"Payment Account",
		filters={"kameti": kameti, "is_active": 1},
		fields=["name", "method", "account_number", "account_title", "bank_name"],
		order_by="display_order asc",
	)
	for r in rows:
		r["id"] = r.pop("name", None)
	return {
		"admin_name": admin_name,
		"admin_phone": admin_phone,
		"kameti_title": committee_title,
		"accounts": rows,
	}


@frappe.whitelist(methods=["POST"])
def submit_payment(
	kameti: str,
	payout_month: int,
	amount: float,
	method: str,
	payment_account: str | None = None,
	receipt_file: str | None = None,
	transaction_id: str | None = None,
	notes: str | None = None,
	voice_note: str | None = None,
):
	user = common.require_session_user()
	mem = common.require_membership(kameti, user)

	payout_month = int(payout_month)
	amount = float(amount)
	if method not in VALID_METHODS:
		frappe.throw("Invalid method.", frappe.ValidationError)
	if method != "cash" and not receipt_file:
		frappe.throw("A receipt is required for this method.", frappe.ValidationError)

	committee_amt = frappe.db.get_value(
		"Kameti Committee", kameti, "installment_amount",
	)
	if committee_amt is None:
		frappe.throw("Installment amount not configured.", frappe.ValidationError)

	# Check if this member has a fractional share in the slot for payout_month
	slot_name = frappe.db.get_value(
		"Payout Slot",
		{"kameti": kameti, "month_index": payout_month},
		"name",
	)
	share_fraction = None
	if slot_name:
		share_fraction = get_member_share_for_slot(slot_name, mem.name)

	expected_amount = float(committee_amt) * (share_fraction if share_fraction else 1.0)
	if abs(amount - expected_amount) > 0.01:
		if share_fraction:
			frappe.throw(
				f"Amount must be {expected_amount:,.0f} (your {share_fraction * 100:.0f}% share of the installment).",
				frappe.ValidationError,
			)
		else:
			frappe.throw(
				"Amount must match the installment amount.", frappe.ValidationError,
			)

	if frappe.db.exists(
		"Installment Payment",
		{"kameti": kameti, "payer": mem.name, "payout_month": payout_month,
		 "status": ("in", ("pending", "approved"))},
	):
		frappe.throw(
			"You already have an active payment for this month.",
			frappe.ValidationError,
		)

	p = frappe.new_doc("Installment Payment")
	p.kameti = kameti
	p.payer = mem.name
	p.payout_month = payout_month
	p.amount = amount
	p.method = method
	p.payment_account = payment_account
	p.transaction_id = transaction_id
	p.receipt_file = receipt_file
	p.voice_note = voice_note
	p.status = "pending"
	p.submitted_on = now_datetime()
	p.insert(ignore_permissions=True)

	admin_user = frappe.db.get_value("Kameti Committee", kameti, "admin")
	_activity(
		recipient=admin_user, kameti=kameti, type_="payment_pending",
		title=f"{mem.display_name} submitted a payment",
		body=f"Amount {amount} via {method}. Tap to review.",
		payload={"payment_id": p.name},
	)
	frappe.db.commit()
	return {
		"payment_id": p.name,
		"status": p.status,
		"awaiting_approval": True,
	}


@frappe.whitelist(methods=["GET"])
def get_payment(payment_id: str):
	user = common.require_session_user()
	p = frappe.get_doc("Installment Payment", payment_id)

	payer_user = frappe.db.get_value("Kameti Membership", p.payer, "user")
	admin = frappe.db.get_value("Kameti Committee", p.kameti, "admin")
	if user not in (payer_user, admin) and "System Manager" not in frappe.get_roles(user):
		frappe.throw("Not authorized to view this payment.", frappe.PermissionError)

	payer = frappe.db.get_value(
		"Kameti Membership", p.payer,
		["display_name", "initials", "avatar_tone"], as_dict=True,
	) or {}
	admin_profile = frappe.db.get_value(
		"Kameti Profile", {"user": admin},
		["display_name", "phone", "last_active"], as_dict=True,
	) or {}
	online = False
	if admin_profile.get("last_active"):
		online = time_diff_in_seconds(now_datetime(), admin_profile["last_active"]) < 120

	return {
		"payment_id": p.name,
		"kameti": p.kameti,
		"payer": {
			"display_name": payer.get("display_name"),
			"initials": payer.get("initials"),
			"tone": payer.get("avatar_tone"),
		},
		"amount": p.amount,
		"method": p.method,
		"transaction_id": p.transaction_id,
		"receipt_url": p.receipt_file,
		"status": p.status,
		"submitted_on": p.submitted_on.isoformat() if p.submitted_on else None,
		"reviewed_on": p.reviewed_on.isoformat() if p.reviewed_on else None,
		"rejection_reason": p.rejection_reason,
		"admin": {
			"display_name": admin_profile.get("display_name") or "Admin",
			"phone": admin_profile.get("phone"),
			"online": online,
		},
	}


@frappe.whitelist(methods=["GET"])
def get_approval_queue(kameti: str):
	common.require_admin(kameti)
	rows = frappe.db.sql(
		"""SELECT p.name, p.amount, p.method, p.transaction_id, p.receipt_file,
				  p.submitted_on, m.display_name, m.initials, m.avatar_tone
		   FROM `tabInstallment Payment` p
		   JOIN `tabKameti Membership` m ON m.name = p.payer
		   WHERE p.kameti=%s AND p.status='pending'
		   ORDER BY p.submitted_on ASC""",
		(kameti,), as_dict=True,
	)
	queue = []
	for r in rows:
		queue.append({
			"payment_id": r.name,
			"payer": {
				"display_name": r.display_name,
				"initials": r.initials,
				"tone": r.avatar_tone,
			},
			"amount": r.amount,
			"method": r.method,
			"transaction_id": r.transaction_id,
			"receipt_url": r.receipt_file,
			"submitted_on": r.submitted_on.isoformat() if r.submitted_on else None,
			"time_ago": pretty_date(r.submitted_on) if r.submitted_on else None,
		})
	return {"queue": queue, "count": len(queue)}


@frappe.whitelist(methods=["POST"])
def approve_payment(payment_id: str):
	p = frappe.get_doc("Installment Payment", payment_id)
	common.require_admin(p.kameti)
	if p.status == "approved":
		return {"ok": True}
	p.status = "approved"
	p.reviewed_by = frappe.session.user
	p.reviewed_on = now_datetime()
	p.save(ignore_permissions=True)

	payer_user = frappe.db.get_value("Kameti Membership", p.payer, "user")
	admin_name = frappe.db.get_value(
		"Kameti Profile", {"user": frappe.session.user}, "display_name",
	) or "Admin"
	if payer_user:
		_activity(
			recipient=payer_user, kameti=p.kameti, type_="payment_approved",
			title="Approved",
			body=f"{admin_name} approved your payment.",
			payload={"payment_id": p.name},
		)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def reject_payment(payment_id: str, reason: str):
	if not reason or not reason.strip():
		frappe.throw("A reason is required.", frappe.ValidationError)
	p = frappe.get_doc("Installment Payment", payment_id)
	common.require_admin(p.kameti)
	p.status = "rejected"
	p.reviewed_by = frappe.session.user
	p.reviewed_on = now_datetime()
	p.rejection_reason = reason.strip()
	p.save(ignore_permissions=True)

	payer_user = frappe.db.get_value("Kameti Membership", p.payer, "user")
	if payer_user:
		_activity(
			recipient=payer_user, kameti=p.kameti, type_="payment_rejected",
			title="Payment rejected",
			body=reason.strip(),
			payload={"payment_id": p.name},
		)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def mark_paid(
	kameti: str,
	membership: str,
	payout_month: int | None = None,
	method: str = "cash",
	amount: float | None = None,
):
	"""Admin records that a member paid out-of-band (e.g. cash / in person).

	Creates an already-approved Installment Payment on the member's behalf for
	the current month (or the given payout_month). Idempotent: if the member
	already has an active payment for that month it is just marked approved.
	"""
	common.require_admin(kameti)

	if not frappe.db.exists(
		"Kameti Membership", {"name": membership, "kameti": kameti, "status": "active"}
	):
		frappe.throw("Member not found in this kameti.", frappe.ValidationError)

	if method not in VALID_METHODS:
		frappe.throw("Invalid method.", frappe.ValidationError)

	committee = frappe.db.get_value(
		"Kameti Committee", kameti,
		["current_month", "installment_amount"], as_dict=True,
	)
	if payout_month is None:
		payout_month = committee.current_month or 0
	payout_month = int(payout_month)
	if not payout_month:
		frappe.throw("This kameti has not started yet.", frappe.ValidationError)

	# A member cannot pay for the month in which they receive the payout.
	slot_recipient = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": payout_month}, "recipient",
	)
	if slot_recipient == membership:
		frappe.throw(
			"This member is the receiver this month.", frappe.ValidationError,
		)

	# Honour a fractional (co-holder) share if present.
	slot_name = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": payout_month}, "name",
	)
	share_fraction = get_member_share_for_slot(slot_name, membership) if slot_name else None
	if amount is None:
		amount = float(committee.installment_amount or 0) * (share_fraction or 1.0)
	amount = float(amount)

	now = now_datetime()
	existing = frappe.db.get_value(
		"Installment Payment",
		{"kameti": kameti, "payer": membership, "payout_month": payout_month,
		 "status": ("in", ("pending", "approved"))},
		"name",
	)
	if existing:
		p = frappe.get_doc("Installment Payment", existing)
		if p.status == "approved":
			return {"ok": True, "payment_id": p.name, "status": p.status}
	else:
		p = frappe.new_doc("Installment Payment")
		p.kameti = kameti
		p.payer = membership
		p.payout_month = payout_month
		p.amount = amount
		p.method = method
		p.submitted_on = now

	p.status = "approved"
	p.reviewed_by = frappe.session.user
	p.reviewed_on = now
	p.rejection_reason = None
	if existing:
		p.save(ignore_permissions=True)
	else:
		p.insert(ignore_permissions=True)

	payer_user = frappe.db.get_value("Kameti Membership", membership, "user")
	admin_name = frappe.db.get_value(
		"Kameti Profile", {"user": frappe.session.user}, "display_name",
	) or "Admin"
	if payer_user:
		_activity(
			recipient=payer_user, kameti=kameti, type_="payment_approved",
			title="Marked as paid",
			body=f"{admin_name} marked your payment as received.",
			payload={"payment_id": p.name},
		)
	frappe.db.commit()
	return {"ok": True, "payment_id": p.name, "status": p.status}


@frappe.whitelist(methods=["POST"])
def unmark_paid(kameti: str, membership: str, payout_month: int | None = None):
	"""Reverse an admin "mark as paid": cancel the member's active payment for
	the month. Used to undo a mistaken tap."""
	common.require_admin(kameti)

	if payout_month is None:
		payout_month = frappe.db.get_value("Kameti Committee", kameti, "current_month") or 0
	payout_month = int(payout_month)

	existing = frappe.db.get_value(
		"Installment Payment",
		{"kameti": kameti, "payer": membership, "payout_month": payout_month,
		 "status": ("in", ("pending", "approved"))},
		"name",
	)
	if not existing:
		return {"ok": True}
	p = frappe.get_doc("Installment Payment", existing)

	# A row the admin created via mark_paid represents no real submission by
	# the member — nothing was ever sent for approval. Delete it outright so
	# an accidental "mark as paid" leaves no trace and, in particular, doesn't
	# permanently freeze the kameti's editable settings (see
	# kameti.api.kameti.settlement_started). A payment the member actually
	# submitted is kept and rejected, preserving the audit trail.
	member_submitted = bool(p.receipt_file or p.transaction_id or p.voice_note)
	if not member_submitted:
		frappe.delete_doc(
			"Installment Payment", p.name, force=True,
			ignore_permissions=True, delete_permanently=True,
		)
		frappe.db.commit()
		return {"ok": True, "deleted": True}

	p.status = "rejected"
	p.reviewed_by = frappe.session.user
	p.reviewed_on = now_datetime()
	p.rejection_reason = "Reverted by admin"
	p.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "deleted": False}


def _activity(recipient, kameti, type_, title, body, payload=None):
	if not recipient:
		return
	a = frappe.new_doc("Activity")
	a.recipient = recipient
	a.kameti = kameti
	a.type = type_
	a.title = title
	a.body = body
	a.payload = frappe.as_json(payload) if payload else None
	a.is_read = 0
	a.insert(ignore_permissions=True)
