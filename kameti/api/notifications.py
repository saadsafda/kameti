"""Notification feed + push token registration. See spec §12."""

import frappe

from kameti.utils import common


@frappe.whitelist(methods=["GET"])
def list(limit: int = 20, before: str | None = None):
	user = common.require_session_user()
	limit = max(1, min(int(limit), 100))
	filters = {"recipient": user}
	if before:
		filters["creation"] = ("<", before)
	items = frappe.get_all(
		"Activity",
		filters=filters,
		fields=["name", "type", "title", "body", "kameti", "payload",
				"is_read", "creation"],
		order_by="creation desc",
		limit=limit,
	)
	out = []
	for i in items:
		out.append({
			"id": i.name,
			"type": i.type,
			"title": i.title,
			"body": i.body,
			"kameti_id": i.kameti,
			"payload": frappe.parse_json(i.payload) if i.payload else None,
			"is_read": bool(i.is_read),
			"created": i.creation.isoformat() if i.creation else None,
		})
	unread = frappe.db.count("Activity", {"recipient": user, "is_read": 0})
	return {"unread_count": unread, "items": out}


@frappe.whitelist(methods=["POST"])
def mark_read(ids):
	user = common.require_session_user()
	if isinstance(ids, str):
		if ids == "all":
			frappe.db.sql(
				"""UPDATE `tabActivity` SET is_read=1
				   WHERE recipient=%s AND is_read=0""",
				(user,),
			)
			frappe.db.commit()
			return {"ok": True}
		ids = frappe.parse_json(ids)
	for i in ids or []:
		rec = frappe.db.get_value("Activity", i, "recipient")
		if rec == user:
			frappe.db.set_value(
				"Activity", i, "is_read", 1, update_modified=False,
			)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def register_fcm(token: str):
	user = common.require_session_user()
	if not token:
		frappe.throw("Token is required.", frappe.ValidationError)
	profile_name = frappe.db.get_value(
		"Kameti Profile", {"user": user}, "name",
	)
	if profile_name:
		frappe.db.set_value(
			"Kameti Profile", profile_name, "fcm_token", token,
			update_modified=False,
		)
		frappe.db.commit()
	return {"ok": True}
