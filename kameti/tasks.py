"""Scheduled jobs for the Kameti app. Wired in hooks.py / scheduler_events."""

import frappe
from frappe.utils import add_months, add_to_date, getdate, now_datetime


def activate_due_kametis():
	"""Daily: activate every `not_started` kameti whose `start_month` has arrived.

	Instant activation on save is handled by the Kameti Committee `before_save`
	controller; this scheduler is the catch-up net for committees scheduled in
	the future and for the day their start_month is reached.
	"""
	today = getdate()
	rows = frappe.get_all(
		"Kameti Committee",
		filters={"cycle_state": "not_started", "archived": 0},
		fields=["name", "start_month"],
	)
	for c in rows:
		if c.start_month and today >= getdate(c.start_month):
			_activate(c.name)
	frappe.db.commit()


def advance_current_month():
	"""On the 1st: bump current_month, mark prior slot paid, mark new slot current.

	Also re-runs the activation check as a defensive belt-and-braces in case
	the daily `activate_due_kametis` job didn't run.
	"""
	today = getdate()
	committees = frappe.get_all(
		"Kameti Committee",
		filters={"cycle_state": ("!=", "completed"), "archived": 0},
		fields=["name", "current_month", "members_count", "start_month",
				"cycle_state"],
	)
	for c in committees:
		if c.cycle_state == "not_started":
			if c.start_month and today >= getdate(c.start_month):
				_activate(c.name)
			continue
		next_month = (c.current_month or 0) + 1
		if next_month > (c.members_count or 0):
			frappe.db.set_value(
				"Kameti Committee", c.name, "cycle_state", "completed",
			)
			continue
		if c.current_month:
			frappe.db.set_value(
				"Payout Slot",
				{"kameti": c.name, "month_index": c.current_month},
				"status", "paid",
			)
		frappe.db.set_value(
			"Kameti Committee", c.name, "current_month", next_month,
		)
		frappe.db.set_value(
			"Payout Slot",
			{"kameti": c.name, "month_index": next_month},
			"status", "current",
		)
	frappe.db.commit()


def _activate(kameti: str):
	frappe.db.set_value(
		"Kameti Committee", kameti,
		{"current_month": 1, "cycle_state": "active"},
	)
	frappe.db.set_value(
		"Payout Slot",
		{"kameti": kameti, "month_index": 1},
		"status", "current",
	)


def expire_otps():
	"""Hard-delete Phone OTP rows older than 24h."""
	cutoff = add_to_date(now_datetime(), hours=-24)
	frappe.db.sql(
		"DELETE FROM `tabPhone OTP` WHERE creation < %s",
		(cutoff,),
	)
	frappe.db.commit()


def send_due_reminders():
	"""3 days before the due date, queue an SMS for each unpaid member."""
	today = getdate()
	committees = frappe.get_all(
		"Kameti Committee",
		filters={"cycle_state": "active", "archived": 0},
		fields=["name", "current_month", "start_month", "installment_amount"],
	)
	for c in committees:
		if not c.current_month or not c.start_month:
			continue
		month_start = add_months(c.start_month, c.current_month - 1)
		due_on = getdate(month_start).replace(day=5)
		if (due_on - today).days != 3:
			continue
		members = frappe.get_all(
			"Kameti Membership",
			filters={"kameti": c.name, "status": "active"},
			pluck="name",
		)
		recipient = frappe.db.get_value(
			"Payout Slot",
			{"kameti": c.name, "month_index": c.current_month},
			"recipient",
		)
		for m in members:
			if m == recipient:
				continue
			paid = frappe.db.exists(
				"Installment Payment",
				{"kameti": c.name, "payer": m, "payout_month": c.current_month,
				 "status": ("in", ("pending", "approved"))},
			)
			if paid:
				continue
			r = frappe.new_doc("Reminder")
			r.kameti = c.name
			r.member = m
			r.channel = "sms"
			r.language = "en"
			r.payload = (
				f"Reminder: your installment of {c.installment_amount} is due in 3 days."
			)
			r.status = "queued"
			r.sent_by = "Administrator"
			r.insert(ignore_permissions=True)
	frappe.db.commit()


def dispatch_reminder_queue():
	"""Pull queued reminders and hand them to the SMS / WhatsApp provider.

	WhatsApp text sends only work inside Meta's 24h customer-service window.
	For business-initiated reminders outside that window, configure an
	approved template and switch the WhatsApp branch to `whatsapp.send_template`.
	"""
	from kameti.utils import sms, whatsapp

	queued = frappe.get_all(
		"Reminder",
		filters={"status": "queued"},
		fields=["name", "channel", "payload", "member"],
		limit=50,
	)
	for q in queued:
		phone = frappe.db.get_value("Kameti Membership", q.member, "phone")
		if not phone:
			frappe.db.set_value("Reminder", q.name, {
				"status": "failed", "error": "No phone on file",
			})
			continue
		try:
			if q.channel == "sms":
				res = sms.send_sms(phone, q.payload)
			elif q.channel == "whatsapp":
				res = whatsapp.send_text(phone, q.payload)
			else:
				frappe.db.set_value("Reminder", q.name, {
					"status": "sent",
					"sent_at": now_datetime(),
				})
				continue
			frappe.db.set_value("Reminder", q.name, {
				"status": "sent",
				"sent_at": now_datetime(),
				"provider_id": res.get("provider_id"),
			})
		except Exception as e:
			frappe.db.set_value("Reminder", q.name, {
				"status": "failed", "error": str(e)[:140],
			})
	frappe.db.commit()


def archive_completed_kametis():
	"""Set cycle_state=completed when all payout slots are paid."""
	rows = frappe.db.sql(
		"""SELECT c.name FROM `tabKameti Committee` c
		   WHERE c.cycle_state = 'active' AND c.archived = 0
			 AND NOT EXISTS (
			   SELECT 1 FROM `tabPayout Slot` s
			   WHERE s.kameti = c.name
				 AND s.status NOT IN ('paid', 'skipped')
			 )""",
		as_dict=True,
	)
	for r in rows:
		frappe.db.set_value("Kameti Committee", r.name, "cycle_state", "completed")
	frappe.db.commit()
