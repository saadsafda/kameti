frappe.ui.form.on("Scheduled Notification", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Preview Audience"), () => {
			frappe.call({
				method:
					"kameti.kameti.doctype.scheduled_notification.scheduled_notification.preview_audience",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __("Counting recipients..."),
				callback: (r) => {
					if (!r.message) return;
					frappe.msgprint({
						title: __("Audience"),
						indicator: r.message.total ? "blue" : "orange",
						message: __(
							"{0} recipient(s) match. {1} have a push token and will get a device notification; the rest see it in-app only.",
							[r.message.total, r.message.with_push_token]
						),
					});
				},
			});
		});

		if (frm.doc.status === "scheduled" && frm.doc.enabled) {
			frm.add_custom_button(__("Send Now"), () => {
				frappe.confirm(
					__("Send this notification to the matching audience right now?"),
					() => {
						frappe.call({
							method:
								"kameti.kameti.doctype.scheduled_notification.scheduled_notification.send_now",
							args: { name: frm.doc.name },
							freeze: true,
							freeze_message: __("Sending..."),
							callback: (r) => {
								frappe.show_alert({
									message: __("Sent to {0} recipient(s).", [
										r.message ? r.message.sent : 0,
									]),
									indicator: "green",
								});
								frm.reload_doc();
							},
						});
					}
				);
			}).addClass("btn-primary");
		}
	},

	audience(frm) {
		// Nudge the admin toward the field the newly-picked audience needs.
		if (frm.doc.audience === "By Country") frm.set_df_property("country_code", "reqd", 1);
	},
});
