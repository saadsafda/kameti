import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today


class KametiCommittee(Document):
	def before_save(self):
		"""Auto-activate when the start month has arrived.

		Fires on every save (desk + API). If the committee is still in
		`not_started` and its `start_month` is on or before today, flip to
		`active` and set `current_month = 1` right here so callers don't have
		to remember to do it. Future-dated committees stay `not_started` and
		get picked up by the `activate_due_kametis` scheduler.
		"""
		if (
			self.cycle_state == "not_started"
			and self.start_month
			and getdate(self.start_month) <= getdate(today())
		):
			self.cycle_state = "active"
			self.current_month = 1
