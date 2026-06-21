"""Row-level permission filters. See spec §15.

`*_query` returns a SQL WHERE-fragment injected into list/report queries.
`*_has_permission` is the single-doc check. System Manager always passes.

Scoping: a user can access a kameti's data when they are either the kameti's
admin (`Kameti Committee.admin == user`) or an active member
(`Kameti Membership(user=user, status='active')`).
"""

import frappe


def _is_system_manager(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return "System Manager" in frappe.get_roles(user)


def _esc(user: str) -> str:
	return frappe.db.escape(user)


def _accessible_kametis_subquery(user: str) -> str:
	return (
		f"SELECT name FROM `tabKameti Committee` WHERE admin = {_esc(user)} "
		f"UNION "
		f"SELECT kameti FROM `tabKameti Membership` "
		f"WHERE user = {_esc(user)} AND status = 'active'"
	)


# ---- Kameti Committee ---------------------------------------------------

def kameti_committee_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return (
		f"(`tabKameti Committee`.admin = {_esc(user)} "
		f"OR `tabKameti Committee`.name IN ("
		f"SELECT kameti FROM `tabKameti Membership` "
		f"WHERE user = {_esc(user)} AND status = 'active'))"
	)


def kameti_committee_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	if doc.admin == user:
		return True
	if permission_type in ("read", None):
		return bool(frappe.db.exists(
			"Kameti Membership",
			{"kameti": doc.name, "user": user, "status": "active"},
		))
	return False


# ---- Kameti Membership --------------------------------------------------

def kameti_membership_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return (
		f"`tabKameti Membership`.kameti IN ({_accessible_kametis_subquery(user)})"
	)


def kameti_membership_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	admin = frappe.db.get_value("Kameti Committee", doc.kameti, "admin")
	if admin == user:
		return True
	if permission_type in ("read", None):
		return bool(frappe.db.exists(
			"Kameti Membership",
			{"kameti": doc.kameti, "user": user, "status": "active"},
		))
	# Members may edit their own membership row only.
	return doc.user == user


# ---- Payout Slot --------------------------------------------------------

def payout_slot_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return f"`tabPayout Slot`.kameti IN ({_accessible_kametis_subquery(user)})"


def payout_slot_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	admin = frappe.db.get_value("Kameti Committee", doc.kameti, "admin")
	if admin == user:
		return True
	if permission_type in ("read", None):
		return bool(frappe.db.exists(
			"Kameti Membership",
			{"kameti": doc.kameti, "user": user, "status": "active"},
		))
	return False


# ---- Payment Account ----------------------------------------------------

def payment_account_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return f"`tabPayment Account`.kameti IN ({_accessible_kametis_subquery(user)})"


def payment_account_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	admin = frappe.db.get_value("Kameti Committee", doc.kameti, "admin")
	if admin == user:
		return True
	if permission_type in ("read", None):
		return bool(frappe.db.exists(
			"Kameti Membership",
			{"kameti": doc.kameti, "user": user, "status": "active"},
		))
	return False


# ---- Installment Payment ----------------------------------------------

def installment_payment_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return (
		f"(`tabInstallment Payment`.kameti IN ("
		f"SELECT name FROM `tabKameti Committee` WHERE admin = {_esc(user)}"
		f") OR `tabInstallment Payment`.payer IN ("
		f"SELECT name FROM `tabKameti Membership` "
		f"WHERE user = {_esc(user)} AND status = 'active'"
		f"))"
	)


def installment_payment_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	if frappe.db.get_value("Kameti Committee", doc.kameti, "admin") == user:
		return True
	payer_user = frappe.db.get_value("Kameti Membership", doc.payer, "user")
	return payer_user == user


# ---- Reminder ---------------------------------------------------------

def reminder_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return (
		f"`tabReminder`.kameti IN ("
		f"SELECT name FROM `tabKameti Committee` WHERE admin = {_esc(user)}"
		f")"
	)


def reminder_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	return frappe.db.get_value("Kameti Committee", doc.kameti, "admin") == user


# ---- Activity ---------------------------------------------------------

def activity_query(user: str) -> str:
	if _is_system_manager(user):
		return ""
	return f"`tabActivity`.recipient = {_esc(user)}"


def activity_has_permission(doc, user, permission_type=None) -> bool:
	if _is_system_manager(user):
		return True
	return doc.recipient == user
