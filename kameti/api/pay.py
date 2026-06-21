"""Pay flow + admin approval queue. See spec §10."""

import frappe
from frappe.utils import now_datetime, pretty_date, time_diff_in_seconds

from kameti.utils import common


VALID_METHODS = ("easypaisa", "jazzcash", "bank", "cash", "other")


@frappe.whitelist(methods=["GET"])
def get_payment_methods(kameti: str):
	common.require_membership_or_admin(kameti)
	admin_user = frappe.db.get_value("Kameti Committee", kameti, "admin")
	admin_name = frappe.db.get_value(
		"Kameti Membership",
		{"kameti": kameti, "user": admin_user, "role": "admin", "status": "active"},
		"display_name",
	) or "Admin"
	rows = frappe.get_all(
		"Payment Account",
		filters={"kameti": kameti, "is_active": 1},
		fields=["name", "method", "account_number", "account_title", "bank_name"],
		order_by="display_order asc",
	)
	for r in rows:
		r["id"] = r.pop("name", None)
	return {"admin_name": admin_name, "accounts": rows}


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
	if committee_amt is None or abs(amount - float(committee_amt)) > 0.01:
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
		["display_name", "last_active"], as_dict=True,
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
