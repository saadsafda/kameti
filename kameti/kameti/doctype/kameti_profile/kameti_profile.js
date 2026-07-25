frappe.ui.form.on("Kameti Profile", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.user) return;

		frm.add_custom_button("Send Birthday Wish", () => {
			send_message_dialog(frm, {
				type: "birthday",
				dialog_title: "Send Birthday Wish",
				default_title: "Happy Birthday!",
				default_body: `Wishing you a wonderful birthday, ${frm.doc.display_name || ""}!`,
			});
		}, "Notify");

		frm.add_custom_button("Send Message", () => {
			send_message_dialog(frm, {
				type: "announcement",
				dialog_title: "Send Message",
				default_title: "",
				default_body: "",
			});
		}, "Notify");
	},
});

function send_message_dialog(frm, { type, dialog_title, default_title, default_body }) {
	const d = new frappe.ui.Dialog({
		title: dialog_title,
		fields: [
			{
				fieldname: "title",
				fieldtype: "Data",
				label: "Title",
				reqd: 1,
				default: default_title,
			},
			{
				fieldname: "body",
				fieldtype: "Small Text",
				label: "Message",
				reqd: 1,
				default: default_body,
			},
		],
		primary_action_label: "Send",
		primary_action(values) {
			frappe.call({
				method: "send_message",
				doc: frm.doc,
				args: {
					title: values.title,
					body: values.body,
					type: type,
				},
				freeze: true,
				callback() {
					frappe.show_alert({
						message: "Notification sent",
						indicator: "green",
					});
					d.hide();
				},
			});
		},
	});
	d.show();
}
