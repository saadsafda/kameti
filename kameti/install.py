import frappe


KAMETI_ROLES = [
	{"role_name": "Kameti Member", "desk_access": 0},
	{"role_name": "Kameti Admin", "desk_access": 0},
]


def after_install():
	_ensure_roles()


def _ensure_roles():
	for role in KAMETI_ROLES:
		if frappe.db.exists("Role", role["role_name"]):
			continue
		doc = frappe.new_doc("Role")
		doc.role_name = role["role_name"]
		doc.desk_access = role["desk_access"]
		doc.insert(ignore_permissions=True)
