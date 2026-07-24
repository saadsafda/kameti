app_name = "kameti"
app_title = "Kameti"
app_publisher = "danerp"
app_description = "Kameti app"
app_email = "muhammadsaadsafdar2005@gmail.com"
app_license = "mit"

# Installation
# ------------

after_install = "kameti.install.after_install"

# Permissions
# -----------

permission_query_conditions = {
	"Kameti Committee": "kameti.api.permission.kameti_committee_query",
	"Kameti Membership": "kameti.api.permission.kameti_membership_query",
	"Payout Slot": "kameti.api.permission.payout_slot_query",
	"Installment Payment": "kameti.api.permission.installment_payment_query",
	"Payment Account": "kameti.api.permission.payment_account_query",
	"Reminder": "kameti.api.permission.reminder_query",
	"Activity": "kameti.api.permission.activity_query",
}

has_permission = {
	"Kameti Committee": "kameti.api.permission.kameti_committee_has_permission",
	"Kameti Membership": "kameti.api.permission.kameti_membership_has_permission",
	"Payout Slot": "kameti.api.permission.payout_slot_has_permission",
	"Installment Payment": "kameti.api.permission.installment_payment_has_permission",
	"Payment Account": "kameti.api.permission.payment_account_has_permission",
	"Reminder": "kameti.api.permission.reminder_has_permission",
	"Activity": "kameti.api.permission.activity_has_permission",
}

# Document Events
# ---------------

doc_events = {
	"User": {
		"after_insert": "kameti.api.profile.ensure_profile_for_user",
	},
	"Activity": {
		"after_insert": "kameti.utils.push.send_for_activity",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"0 0 1 * *": [
			"kameti.tasks.advance_current_month",
		],
	},
	"hourly": [
		"kameti.tasks.dispatch_reminder_queue",
	],
	"daily": [
		"kameti.tasks.activate_due_kametis",
		"kameti.tasks.expire_otps",
		"kameti.tasks.send_due_reminders",
		"kameti.tasks.send_due_push_notifications",
		"kameti.tasks.archive_completed_kametis",
	],
}

# Authentication
# --------------

# Allow these whitelisted endpoints to be called without a session
# (the auth.* endpoints handle their own rate limiting and OTP verification).
# Listed for documentation; the actual decorator on each method enforces this.

# Fixtures
# --------

fixtures = []
