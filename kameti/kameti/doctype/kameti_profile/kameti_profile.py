import frappe
from frappe.model.document import Document


class KametiProfile(Document):
	@frappe.whitelist()
	def send_message(self, title: str, body: str, type: str = "announcement"):
		"""Desk-only: push a custom Activity to this profile's user.

		Called from the "Send Message" / "Send Birthday Wish" buttons on the
		Kameti Profile form. Reuses the Activity -> FCM pipeline (see
		Activity.after_insert in hooks.py), so it also shows up in the member's
		in-app notification feed.
		"""
		frappe.only_for("System Manager")
		title = (title or "").strip()
		body = (body or "").strip()
		if not title or not body:
			frappe.throw("Title and message are required.", frappe.ValidationError)
		if not self.user:
			frappe.throw("This profile has no linked user.", frappe.ValidationError)
		if type not in ("announcement", "birthday"):
			type = "announcement"

		a = frappe.new_doc("Activity")
		a.recipient = self.user
		a.type = type
		a.title = title[:140]
		a.body = body[:500]
		a.is_read = 0
		a.insert(ignore_permissions=True)
		frappe.db.commit()
		return {"ok": True}
