"""Scheduled jobs for the Kameti app. Wired in hooks.py / scheduler_events."""

import frappe
from frappe.utils import add_to_date, fmt_money, getdate, now_datetime

from kameti.utils import common


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
		due_on = common.due_date_for_month(c.start_month, c.current_month)
		if not due_on or (due_on - today).days != 3:
			continue
		for row in _unpaid_members(c.name, c.current_month):
			if row["state"] == "rejected":
				payload = (
					f"Your payment of {c.installment_amount} was rejected"
					+ (f' ({row["reason"]})' if row["reason"] else "")
					+ ". Please resend proof — due in 3 days."
				)
			else:
				payload = (
					f"Reminder: your installment of {c.installment_amount} is due in 3 days."
				)
			r = frappe.new_doc("Reminder")
			r.kameti = c.name
			r.member = row["membership"]
			r.channel = "sms"
			r.language = "en"
			r.payload = payload
			r.status = "queued"
			r.sent_by = "Administrator"
			r.insert(ignore_permissions=True)
	frappe.db.commit()


def send_due_push_notifications():
	"""Daily: in-app + push notification 2 days before the due date, and on it.

	Creates an Activity per unpaid member; the `Activity.after_insert` hook in
	hooks.py fans it out to FCM. Idempotent — if the job runs twice in one day
	the second pass is a no-op, since an Activity of the same type already
	exists for that member today.
	"""
	today = getdate()
	committees = frappe.get_all(
		"Kameti Committee",
		filters={"cycle_state": "active", "archived": 0},
		fields=["name", "title", "current_month", "start_month",
				"installment_amount"],
	)
	for c in committees:
		due_on = common.due_date_for_month(c.start_month, c.current_month)
		if not due_on:
			continue
		days_left = (due_on - today).days
		if days_left not in (2, 0):
			continue

		amount = fmt_money(c.installment_amount or 0, currency="PKR")
		when = f"on {due_on.strftime('%d %b')}" if days_left == 2 else "today"

		for row in _unpaid_members(c.name, c.current_month):
			user = frappe.db.get_value(
				"Kameti Membership", row["membership"], "user",
			)
			if not user:
				continue

			if row["state"] == "rejected":
				# Their receipt was turned down — resubmitting is the action,
				# so say that instead of implying they never paid.
				type_ = "payment_due_rejected"
				title = (
					"Payment rejected — resubmit in 2 days"
					if days_left == 2
					else "Payment rejected — due today"
				)
				body = f"Your payment for {c.title} was rejected"
				if row["reason"]:
					body += f' ({row["reason"]})'
				body += f". Please resend proof of {amount} — due {when}."
			else:
				type_ = "payment_due" if days_left == 2 else "payment_overdue"
				title = (
					"Installment due in 2 days"
					if days_left == 2
					else "Installment due today"
				)
				body = f"Your {amount} installment for {c.title} is due {when}."

			# Don't re-notify if this member already got this alert today.
			# (Keyed on the day rather than the payload: the daily job only
			# fires each type once per cycle anyway, since days_left is
			# unique per day.)
			if frappe.db.exists("Activity", {
				"recipient": user, "kameti": c.name, "type": type_,
				"creation": (">=", today),
			}):
				continue

			a = frappe.new_doc("Activity")
			a.recipient = user
			a.kameti = c.name
			a.type = type_
			a.title = title
			a.body = body
			a.payload = frappe.as_json({
				"kameti_id": c.name,
				"month": c.current_month,
				"due_on": due_on.isoformat(),
				"state": row["state"],
			})
			a.is_read = 0
			a.insert(ignore_permissions=True)
	frappe.db.commit()


def _unpaid_members(kameti: str, month: int) -> list[dict]:
	"""Active memberships still owing for `month`, and why.

	Excludes that month's payout recipient (they receive, not pay) and anyone
	with a pending or approved payment — a pending receipt is awaiting admin
	review, so nagging them would be wrong.

	Each row is `{"membership", "state", "reason"}` where `state` is:
	  "none"     — never submitted a receipt.
	  "rejected" — submitted, but the admin rejected it; `reason` holds the
	               admin's rejection note so the message can reference it.
	"""
	members = frappe.get_all(
		"Kameti Membership",
		filters={"kameti": kameti, "status": "active"},
		pluck="name",
	)
	recipient = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month}, "recipient",
	)
	out = []
	for m in members:
		if m == recipient:
			continue
		if frappe.db.exists("Installment Payment", {
			"kameti": kameti, "payer": m, "payout_month": month,
			"status": ("in", ("pending", "approved")),
		}):
			continue
		# No active payment. If their most recent attempt was rejected, tell
		# them that specifically rather than "you haven't paid".
		last_rejected = frappe.get_all(
			"Installment Payment",
			filters={"kameti": kameti, "payer": m, "payout_month": month,
					 "status": "rejected"},
			fields=["rejection_reason"],
			order_by="reviewed_on desc",
			limit=1,
		)
		if last_rejected:
			out.append({
				"membership": m,
				"state": "rejected",
				"reason": (last_rejected[0].get("rejection_reason") or "").strip(),
			})
		else:
			out.append({"membership": m, "state": "none", "reason": ""})
	return out


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


def send_birthday_wishes():
	"""Daily: wish every active member whose date_of_birth is today.

	Matches month+day only (year is birth year, not this year). Creates one
	Activity per matching Kameti Profile; the `Activity.after_insert` hook in
	hooks.py fans it out to FCM, same pipeline as the payment-due reminders.
	Idempotent — skips a user who already got a `birthday` Activity today, so
	re-running the job the same day is a no-op.
	"""
	today = getdate()
	profiles = frappe.get_all(
		"Kameti Profile",
		filters={"date_of_birth": ("is", "set")},
		fields=["user", "display_name", "date_of_birth"],
	)
	for p in profiles:
		dob = getdate(p.date_of_birth)
		if (dob.month, dob.day) != (today.month, today.day):
			continue
		if frappe.db.exists("Activity", {
			"recipient": p.user, "type": "birthday", "creation": (">=", today),
		}):
			continue
		a = frappe.new_doc("Activity")
		a.recipient = p.user
		a.type = "birthday"
		a.title = "Happy Birthday!"
		a.body = f"Wishing you a wonderful birthday, {p.display_name}!"
		a.payload = frappe.as_json({"user": p.user})
		a.is_read = 0
		a.insert(ignore_permissions=True)
	frappe.db.commit()


def dispatch_scheduled_notifications():
	"""Hourly: fire every due `Scheduled Notification`.

	Due means enabled, still `scheduled`, and `scheduled_on` has passed. Each
	row resolves its own audience and inserts Activities; the
	`Activity.after_insert` hook fans them out to FCM. A row that raises is
	marked `failed` with the error rather than aborting the whole batch.
	"""
	due = frappe.get_all(
		"Scheduled Notification",
		filters={
			"enabled": 1,
			"status": "scheduled",
			"scheduled_on": ("<=", now_datetime()),
		},
		pluck="name",
	)
	for name in due:
		doc = frappe.get_doc("Scheduled Notification", name)
		try:
			count = doc.dispatch()
			doc.mark_sent(count)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Scheduled Notification {name} failed",
				message=frappe.get_traceback(),
			)
			frappe.db.set_value("Scheduled Notification", name, {
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
