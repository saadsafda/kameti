"""Reminders / WhatsApp. See spec §11."""

import frappe
from frappe.utils import add_months, formatdate

from kameti.utils import common, templates


VALID_CHANNELS = ("sms", "whatsapp", "push", "in_app")


@frappe.whitelist(methods=["GET"])
def get_template(kameti: str, membership_id: str, channel: str, language: str = "en"):
	common.require_admin(kameti)
	if channel not in VALID_CHANNELS:
		frappe.throw("Invalid channel.", frappe.ValidationError)

	committee = frappe.get_doc("Kameti Committee", kameti)
	mem = frappe.db.get_value(
		"Kameti Membership", membership_id,
		["display_name", "urdu_name"], as_dict=True,
	)
	if not mem:
		frappe.throw("Member not found.", frappe.DoesNotExistError)

	accts = frappe.get_all(
		"Payment Account",
		filters={"kameti": kameti, "is_active": 1},
		fields=["method", "account_number", "bank_name"],
		order_by="display_order asc",
		limit=1,
	)
	account = accts[0] if accts else {"method": "", "account_number": ""}
	admin_name = frappe.db.get_value(
		"Kameti Profile", {"user": committee.admin}, "display_name",
	) or "Admin"

	cm = committee.current_month or 1
	due_str = "soon"
	if committee.start_month:
		due_date = add_months(committee.start_month, cm - 1)
		due_str = formatdate(due_date, "d MMM")

	name = mem.urdu_name if language == "ur" and mem.urdu_name else mem.display_name
	body = templates.render_reminder(
		channel=channel,
		language=language,
		member_name=name,
		kameti_title=committee.title,
		amount=committee.installment_amount,
		due_date=due_str,
		admin_name=admin_name,
		payment_account=account,
	)
	return {"language": language, "channel": channel, "subject": None, "body": body}


@frappe.whitelist(methods=["POST"])
def send(
	kameti: str,
	membership_ids: list | str,
	channel: str,
	language: str = "en",
	override_body: str | None = None,
):
	common.require_admin(kameti)
	if channel not in VALID_CHANNELS:
		frappe.throw("Invalid channel.", frappe.ValidationError)
	if isinstance(membership_ids, str):
		membership_ids = frappe.parse_json(membership_ids)
	if not isinstance(membership_ids, list) or not membership_ids:
		frappe.throw("membership_ids is required.", frappe.ValidationError)

	sender = frappe.session.user
	reminder_ids: list[str] = []
	for mid in membership_ids:
		mem = frappe.db.get_value(
			"Kameti Membership", mid,
			["name", "user"], as_dict=True,
		)
		if not mem:
			continue
		if override_body:
			body = override_body
		else:
			tmpl = get_template(
				kameti=kameti, membership_id=mid,
				channel=channel, language=language,
			)
			body = tmpl["body"]

		r = frappe.new_doc("Reminder")
		r.kameti = kameti
		r.member = mid
		r.channel = channel
		r.language = language
		r.payload = body
		r.status = "queued"
		r.sent_by = sender
		r.insert(ignore_permissions=True)
		reminder_ids.append(r.name)

		if mem.user and channel == "in_app":
			_activity(
				recipient=mem.user, kameti=kameti,
				type_="reminder_received",
				title="Reminder",
				body=body,
			)
	frappe.db.commit()
	return {"queued": len(reminder_ids), "reminder_ids": reminder_ids}


@frappe.whitelist(methods=["GET"])
def recent(kameti: str):
	common.require_admin(kameti)
	rows = frappe.db.sql(
		"""SELECT r.name, r.member, r.channel, r.status, r.sent_at,
				  m.display_name
		   FROM `tabReminder` r
		   JOIN `tabKameti Membership` m ON m.name = r.member
		   WHERE r.kameti=%s
		   ORDER BY r.creation DESC LIMIT 50""",
		(kameti,), as_dict=True,
	)
	items = []
	for r in rows:
		items.append({
			"id": r.name,
			"member": {"membership_id": r.member, "display_name": r.display_name},
			"channel": r.channel,
			"status": r.status,
			"sent_at": r.sent_at.isoformat() if r.sent_at else None,
		})
	return {"items": items}


def _activity(recipient, kameti, type_, title, body, payload=None):
	a = frappe.new_doc("Activity")
	a.recipient = recipient
	a.kameti = kameti
	a.type = type_
	a.title = title
	a.body = body
	a.payload = frappe.as_json(payload) if payload else None
	a.is_read = 0
	a.insert(ignore_permissions=True)
