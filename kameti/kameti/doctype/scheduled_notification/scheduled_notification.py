"""Admin-authored notifications that fire at a chosen date/time.

Resolves an audience (everyone / a country / a hand-picked list / one kameti's
members) into a set of Users, then inserts one Activity per user. The
`Activity.after_insert` hook in hooks.py fans each row out to FCM, so this
DocType never talks to Firebase directly — same pipeline as the birthday and
payment-due jobs in tasks.py.

Dispatch is driven by `kameti.tasks.dispatch_scheduled_notifications`, wired to
the hourly scheduler.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime


class ScheduledNotification(Document):
	def validate(self):
		self._validate_audience()
		self._validate_schedule()

	def _validate_audience(self):
		if self.audience == "By Country" and not self.country_code:
			frappe.throw(_("Select a country for a country-targeted notification."))
		if self.audience == "Kameti Members" and not self.kameti:
			frappe.throw(_("Select a kameti for a member-targeted notification."))
		if self.audience == "Specific Users":
			if not self.recipients:
				frappe.throw(_("Add at least one recipient."))
			seen = set()
			for row in self.recipients:
				if row.user in seen:
					frappe.throw(_("{0} is listed twice.").format(row.user))
				seen.add(row.user)
				# Denormalised for grid readability; the phone is also the only
				# way an admin can tell two same-named users apart.
				profile = frappe.db.get_value(
					"Kameti Profile", {"user": row.user},
					["display_name", "phone"], as_dict=True,
				)
				if profile:
					row.display_name = profile.display_name
					row.phone = profile.phone

		# Clear fields the chosen audience doesn't use, so a stale value from an
		# earlier edit can't silently narrow the send.
		if self.audience != "By Country":
			self.country_code = None
		if self.audience != "Kameti Members":
			self.kameti = None
		if self.audience != "Specific Users":
			self.recipients = []

	def _validate_schedule(self):
		if not self.scheduled_on:
			return
		# Only guard on create / reschedule — an already-sent row legitimately
		# holds a past datetime.
		if self.is_new() and get_datetime(self.scheduled_on) < now_datetime():
			frappe.msgprint(
				_("This send time is in the past; it will go out on the next scheduler run."),
				indicator="orange", alert=True,
			)

	# ------------------------------------------------------------------
	# Audience resolution
	# ------------------------------------------------------------------

	def resolve_recipients(self) -> list[dict]:
		"""Return `[{"user", "display_name"}]` for this notification's audience.

		Only profiles with a linked User are returned — ledger memberships have
		`user=None` and Activity.recipient is mandatory, so they can't receive.
		"""
		if self.audience == "Specific Users":
			users = [r.user for r in self.recipients if r.user]
			if not users:
				return []
			return frappe.get_all(
				"Kameti Profile",
				filters={"user": ("in", users)},
				fields=["user", "display_name"],
			)

		if self.audience == "Kameti Members":
			members = frappe.get_all(
				"Kameti Membership",
				filters={"kameti": self.kameti, "status": "active"},
				pluck="user",
			)
			users = [u for u in members if u]
			if not users:
				return []
			return frappe.get_all(
				"Kameti Profile",
				filters={"user": ("in", users)},
				fields=["user", "display_name"],
			)

		filters = {"user": ("is", "set")}
		if self.audience == "By Country":
			# country_code is stored as "+92 Pakistan"; the dial prefix is the
			# leading token and phone is validated E.164, so a prefix LIKE is a
			# sound country filter.
			prefix = (self.country_code or "").split()[0]
			if not prefix:
				return []
			filters["phone"] = ("like", f"{prefix}%")

		return frappe.get_all(
			"Kameti Profile", filters=filters, fields=["user", "display_name"],
		)

	# ------------------------------------------------------------------
	# Dispatch
	# ------------------------------------------------------------------

	def dispatch(self) -> int:
		"""Insert one Activity per resolved recipient. Returns the count sent.

		Idempotent per calendar day: a recipient who already has an Activity
		from this notification today is skipped, so a re-run (or an annual
		repeat that lands twice) won't double-notify.
		"""
		today = getdate()
		sent = 0
		for person in self.resolve_recipients():
			if frappe.db.exists("Activity", {
				"recipient": person.user,
				"type": self.activity_type,
				"title": self.notification_title,
				"creation": (">=", today),
			}):
				continue

			body = (self.message or "").replace(
				"{name}", (person.display_name or "").strip() or "there",
			)

			a = frappe.new_doc("Activity")
			a.recipient = person.user
			a.kameti = self.kameti or None
			a.type = self.activity_type
			a.title = self.notification_title
			a.body = body
			a.payload = frappe.as_json({
				"scheduled_notification": self.name,
				"audience": self.audience,
			})
			a.is_read = 0
			a.insert(ignore_permissions=True)
			sent += 1
		return sent

	def mark_sent(self, count: int):
		"""Stamp delivery state, and roll an annual repeat to next year."""
		self.db_set("sent_count", count, update_modified=False)
		self.db_set("last_sent_on", now_datetime(), update_modified=False)
		self.db_set("error", None, update_modified=False)

		if self.repeat_annually:
			# Stays `scheduled` and moves to next year's date, so the same row
			# serves every year.
			self.db_set(
				"scheduled_on",
				add_to_date(get_datetime(self.scheduled_on), years=1),
				update_modified=False,
			)
		else:
			self.db_set("status", "sent", update_modified=False)


@frappe.whitelist()
def send_now(name: str):
	"""Desk button — dispatch immediately, ignoring `scheduled_on`."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc("Scheduled Notification", name)
	count = doc.dispatch()
	doc.mark_sent(count)
	frappe.db.commit()
	return {"sent": count}


@frappe.whitelist()
def preview_audience(name: str):
	"""Desk button — how many users would this reach, without sending."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc("Scheduled Notification", name)
	people = doc.resolve_recipients()
	with_token = frappe.db.count(
		"Kameti Profile",
		{"user": ("in", [p.user for p in people] or [""]), "fcm_token": ("is", "set")},
	) if people else 0
	return {"total": len(people), "with_push_token": with_token}
