"""The changes the copilot knows how to make, and the endpoints behind them.

Each builder turns a plain request ("add a PO number field to Sales Order")
into rows of a VS Change Set. Nothing here writes to the site: the change set
does that, in one transaction, keeping a snapshot so it can be undone.

Everything a builder produces lands in the customisation layer — Custom Field,
Property Setter, Client Script, Report, Workflow — so a mistake costs a click
of Undo, never a customer's data.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from frappe_vs.frappe_vs.doctype.vs_change_set.vs_change_set import ALLOWED

FIELDTYPES = (
	"Data",
	"Int",
	"Float",
	"Currency",
	"Percent",
	"Check",
	"Select",
	"Link",
	"Date",
	"Datetime",
	"Time",
	"Text",
	"Small Text",
	"Long Text",
	"Text Editor",
	"Attach",
	"Attach Image",
)


def _staff():
	if "System Manager" not in frappe.get_roles() and frappe.session.user != "Administrator":
		frappe.throw(_("Only System Managers can change this site."), frappe.PermissionError)


def add_field(
	doctype: str,
	label: str,
	fieldtype: str = "Data",
	options: str | None = None,
	insert_after: str | None = None,
	reqd: bool = False,
	fieldname: str | None = None,
) -> dict:
	"""A new field on an existing form, as a Custom Field."""
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(_("There is no doctype called {0}.").format(doctype))
	if fieldtype not in FIELDTYPES:
		frappe.throw(_("{0} is not a field type this can add.").format(fieldtype))
	if fieldtype == "Link" and not frappe.db.exists("DocType", options or ""):
		frappe.throw(_("A Link field needs the doctype it points at."))

	fieldname = fieldname or frappe.scrub(label)
	meta = frappe.get_meta(doctype)
	if meta.get_field(fieldname):
		frappe.throw(_("{0} already has a field called {1}.").format(doctype, fieldname))
	if insert_after:
		insert_after = _resolve(meta, insert_after)

	return {
		"action": "Create",
		"ref_doctype": "Custom Field",
		"ref_name": f"{doctype}-{fieldname}",
		"summary": _("Add {0} ({1}) to {2}").format(label, fieldtype, doctype),
		"payload": json.dumps(
			{
				"dt": doctype,
				"fieldname": fieldname,
				"label": label,
				"fieldtype": fieldtype,
				"options": options,
				"insert_after": insert_after or _last_field(meta),
				"reqd": 1 if reqd else 0,
			}
		),
	}


def set_property(doctype: str, fieldname: str, prop: str, value, property_type: str = "Data") -> dict:
	"""Change one thing about an existing field: its label, whether it is required, hidden."""
	meta = frappe.get_meta(doctype)
	if fieldname:
		fieldname = _resolve(meta, fieldname)

	name = f"{doctype}-{fieldname}-{prop}"
	payload = {
		"doctype_or_field": "DocField" if fieldname else "DocType",
		"doc_type": doctype,
		"field_name": fieldname,
		"property": prop,
		"value": value,
		"property_type": property_type,
	}
	if frappe.db.exists("Property Setter", name):
		return {
			"action": "Update",
			"ref_doctype": "Property Setter",
			"ref_name": name,
			"summary": _("Set {0} of {1} to {2}").format(prop, fieldname or doctype, value),
			"payload": json.dumps({"value": value}),
		}
	return {
		"action": "Create",
		"ref_doctype": "Property Setter",
		"ref_name": name,
		"summary": _("Set {0} of {1} to {2}").format(prop, fieldname or doctype, value),
		"payload": json.dumps(payload),
	}


def create_report(title: str, ref_doctype: str, query: str, is_standard: str = "No") -> dict:
	"""A Query Report: one SELECT, shown as a report in the desk."""
	if not frappe.db.exists("DocType", ref_doctype):
		frappe.throw(_("There is no doctype called {0}.").format(ref_doctype))
	stripped = (query or "").strip().lower()
	if not stripped.startswith("select"):
		frappe.throw(_("A report's query has to be a SELECT."))
	for word in ("insert", "update", "delete", "drop", "alter", "truncate", "grant"):
		if f" {word} " in f" {stripped} ":
			frappe.throw(_("A report's query cannot contain {0}.").format(word.upper()))

	return {
		"action": "Create",
		"ref_doctype": "Report",
		"ref_name": title,
		"summary": _("Report: {0}").format(title),
		"payload": json.dumps(
			{
				"report_name": title,
				"ref_doctype": ref_doctype,
				"report_type": "Query Report",
				"is_standard": is_standard,
				"query": query,
				"module": frappe.db.get_value("DocType", ref_doctype, "module"),
			}
		),
	}


def _resolve(meta, field: str) -> str:
	"""Take a fieldname, or the label someone reads on the form."""
	if meta.get_field(field):
		return field
	folded = field.strip().lower()
	for df in meta.fields:
		if (df.label or "").strip().lower() == folded:
			return df.fieldname
	frappe.throw(
		_("{0} has no field called {1}. Use the fieldname, for example item_name.").format(
			meta.name, field
		)
	)


def _last_field(meta) -> str | None:
	fields = [f.fieldname for f in meta.fields if f.fieldtype not in ("Section Break", "Column Break")]
	return fields[-1] if fields else None


@frappe.whitelist()
def propose(title: str, request: str = "", changes: str | list | None = None) -> dict:
	"""Write a change set down. Nothing happens to the site until it is applied."""
	_staff()
	rows = frappe.parse_json(changes) or []
	if not rows:
		frappe.throw(_("A change set needs at least one change."))

	doc = frappe.get_doc(
		{
			"doctype": "VS Change Set",
			"title": title,
			"request": request,
			"changes": rows,
		}
	).insert(ignore_permissions=True)
	return _as_dict(doc)


@frappe.whitelist()
def apply(name: str) -> dict:
	_staff()
	doc = frappe.get_doc("VS Change Set", name)
	doc.apply()
	return _as_dict(doc.reload())


@frappe.whitelist()
def undo(name: str) -> dict:
	_staff()
	doc = frappe.get_doc("VS Change Set", name)
	doc.undo()
	return _as_dict(doc.reload())


@frappe.whitelist()
def history(limit: int = 20) -> list[dict]:
	_staff()
	return frappe.get_all(
		"VS Change Set",
		fields=["name", "title", "status", "applied_at", "applied_by"],
		order_by="creation desc",
		limit=limit,
	)


def _as_dict(doc) -> dict:
	return {
		"name": doc.name,
		"title": doc.title,
		"status": doc.status,
		"request": doc.request,
		"error": doc.error,
		"changes": [
			{
				"action": row.action,
				"doctype": row.ref_doctype,
				"name": row.ref_name,
				"summary": row.summary,
			}
			for row in doc.changes
		],
		"allowed": list(ALLOWED),
	}
