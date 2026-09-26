"""A set of changes to this site, applied together and undoable together.

Frappe VS can change a site — add a field, rename a label, make a report. Doing
that from a chat is only safe if every change is written down first, applied in
one transaction, and can be put back exactly as it was. That is what this
record is: the request, the changes it produced, and how each record looked
before.

Two rules keep it honest:

* Only the doctypes in ``ALLOWED`` can be touched. They are the customisation
  layer — fields, properties, scripts, reports, workflows — never a customer's
  invoices or stock.
* Nothing is applied twice, and nothing is undone twice: the status says which
  state the set is in, and the undo restores from the snapshot taken at apply
  time, not from whatever the model thinks it did.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document

# The customisation layer, and nothing else. Business data is never in reach.
ALLOWED = (
	"Custom Field",
	"Property Setter",
	"Client Script",
	"Server Script",
	"Report",
	"Workflow",
	"Workflow State",
	"Workflow Action Master",
	"Notification",
	"Print Format",
	"Dashboard Chart",
	"Number Card",
	"Translation",
)


class VSChangeSet(Document):
	def validate(self):
		for row in self.changes:
			if row.ref_doctype not in ALLOWED:
				frappe.throw(
					_("Frappe VS does not change {0}. It only changes customisations.").format(
						frappe.bold(row.ref_doctype)
					)
				)
			if row.action in ("Update", "Delete") and not row.ref_name:
				frappe.throw(_("A change that edits or removes a record needs its name."))

	@frappe.whitelist()
	def apply(self) -> dict:
		"""Write every change, remembering how each record looked first."""
		if self.status == "Applied":
			frappe.throw(_("This change set has already been applied."))

		# Writing a Custom Field commits, so a database rollback cannot take the
		# earlier changes back. The snapshots can: undo what was done, in reverse.
		done = []
		try:
			for row in self.changes:
				row.before = json.dumps(_snapshot(row), default=str)
				_write(row)
				done.append(row)
		except Exception as e:
			for row in reversed(done):
				try:
					_restore(row)
				except Exception:
					frappe.log_error(
						f"{self.name}: could not undo {row.ref_doctype} {row.ref_name}",
						"Frappe VS change set",
					)
			self.reload()
			self.db_set({"status": "Failed", "error": str(e)[:1000]})
			raise

		self.db_set(
			{
				"status": "Applied",
				"error": None,
				"applied_at": frappe.utils.now(),
				"applied_by": frappe.session.user,
			}
		)
		for row in self.changes:
			row.db_update()
		frappe.clear_cache()
		return {"status": self.status, "changes": len(self.changes)}

	@frappe.whitelist()
	def undo(self) -> dict:
		"""Put every record back the way the snapshot says it was."""
		if self.status != "Applied":
			frappe.throw(_("Only an applied change set can be undone."))

		try:
			# Backwards: a later change may depend on an earlier one.
			for row in reversed(self.changes):
				_restore(row)
		except Exception as e:
			self.reload()
			self.db_set({"status": "Failed", "error": str(e)[:1000]})
			raise

		self.db_set({"status": "Undone", "error": None})
		frappe.clear_cache()
		return {"status": self.status}


def _snapshot(row) -> dict | None:
	"""The record as it is now, or None when it does not exist yet."""
	if row.action == "Create":
		return None
	if not frappe.db.exists(row.ref_doctype, row.ref_name):
		return None
	return frappe.get_doc(row.ref_doctype, row.ref_name).as_dict(no_nulls=True)


def _write(row) -> None:
	payload = frappe.parse_json(row.payload) or {}
	if row.action == "Create":
		doc = frappe.get_doc({"doctype": row.ref_doctype, **payload})
		doc.insert(ignore_permissions=True)
		# The name is only known once it is inserted, and the undo needs it.
		row.ref_name = doc.name
		return

	doc = frappe.get_doc(row.ref_doctype, row.ref_name)
	if row.action == "Delete":
		doc.delete(ignore_permissions=True)
		return

	doc.update(payload)
	doc.save(ignore_permissions=True)


def _restore(row) -> None:
	before = frappe.parse_json(row.before)
	exists = row.ref_name and frappe.db.exists(row.ref_doctype, row.ref_name)

	if before is None:
		# It did not exist before this change set, so it should not exist now.
		if exists:
			frappe.delete_doc(row.ref_doctype, row.ref_name, ignore_permissions=True, force=True)
		return

	if not exists:
		frappe.get_doc(before).insert(ignore_permissions=True, set_name=row.ref_name)
		return

	doc = frappe.get_doc(row.ref_doctype, row.ref_name)
	doc.update({k: v for k, v in before.items() if k not in ("name", "doctype", "creation", "owner")})
	doc.save(ignore_permissions=True)
