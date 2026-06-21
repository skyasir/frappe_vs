"""Registry of framework-safe objects that Frappe VS edits in **Mode B**.

Mode B (developer_mode OFF) is a creation + editing console for DB-stored
objects. Every object is edited in Monaco and saved through the normal DocType
API, so native validation and ``safe_exec`` checks always fire.

Each object type declares how it is edited:

* ``edit = "field"`` — a single code field is shown in Monaco (e.g. a Server
  Script's ``script``). ``field`` / ``language`` name it.
* ``edit = "json"`` — the whole document is shown as pretty JSON (e.g. a Custom
  DocType definition). Saving parses the JSON and re-applies it to the doc.

``Report`` is special: the field depends on ``report_type`` (Query → SQL,
Script → Python), resolved per record by :func:`resolve_field`.

The registry is also the single allow-list: :func:`get_config` refuses any
object type not listed here, so the editor can never touch arbitrary doctypes.
"""

from __future__ import annotations

import frappe
from frappe import _

# Document keys that are framework-managed; stripped before showing JSON and
# never applied back on save.
VOLATILE_KEYS = {
	"creation",
	"modified",
	"modified_by",
	"owner",
	"docstatus",
	"idx",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
	"__onload",
	"__last_sync_on",
	"__islocal",
	"__unsaved",
}


OBJECT_TYPES: dict[str, dict] = {
	# ---- single code-field objects --------------------------------------
	"Server Script": {
		"edit": "field",
		"field": "script",
		"language": "python",
		"icon": "λ",
		"creatable": True,
		"new_fields": [
			{"fieldname": "name", "label": "Name", "fieldtype": "Data", "reqd": 1},
			{
				"fieldname": "script_type",
				"label": "Script Type",
				"fieldtype": "Select",
				"options": "API\nScheduler Event\nDocType Event\nPermission Query",
				"default": "API",
				"reqd": 1,
			},
			{
				"fieldname": "reference_doctype",
				"label": "Reference DocType",
				"fieldtype": "Link",
				"options": "DocType",
			},
		],
	},
	"Client Script": {
		"edit": "field",
		"field": "script",
		"language": "javascript",
		"icon": "{}",
		"creatable": True,
		"new_fields": [
			{"fieldname": "name", "label": "Name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "dt", "label": "DocType", "fieldtype": "Link", "options": "DocType", "reqd": 1},
			{
				"fieldname": "view",
				"label": "View",
				"fieldtype": "Select",
				"options": "Form\nList",
				"default": "Form",
			},
		],
	},
	"Print Format": {
		"edit": "field",
		"field": "html",
		"language": "html",
		"icon": "⎙",
		"list_filters": {"custom": 1},
		"creatable": True,
		"new_fields": [
			{"fieldname": "name", "label": "Name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "doc_type", "label": "DocType", "fieldtype": "Link", "options": "DocType", "reqd": 1},
		],
	},
	"Web Template": {
		"edit": "field",
		"field": "template",
		"language": "html",
		"icon": "</>",
		"creatable": True,
		"new_fields": [
			{"fieldname": "name", "label": "Name", "fieldtype": "Data", "reqd": 1},
			{
				"fieldname": "type",
				"label": "Type",
				"fieldtype": "Select",
				"options": "Component\nSection\nNavbar\nFooter",
				"default": "Component",
			},
		],
	},
	"Notification": {
		"edit": "field",
		"field": "message",
		"language": "html",
		"label_field": "subject",
		"icon": "✉",
		"creatable": True,
		"new_fields": [
			{"fieldname": "subject", "label": "Subject", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "document_type", "label": "Document Type", "fieldtype": "Link", "options": "DocType", "reqd": 1},
			{
				"fieldname": "event",
				"label": "Event",
				"fieldtype": "Select",
				"options": "New\nSave\nSubmit\nCancel\nValue Change\nDays After\nDays Before",
				"default": "New",
				"reqd": 1,
			},
			{
				"fieldname": "channel",
				"label": "Channel",
				"fieldtype": "Select",
				"options": "Email\nSystem Notification\nSlack\nSMS",
				"default": "Email",
				"reqd": 1,
			},
		],
	},
	"Web Page": {
		"edit": "field",
		"field": "main_section_html",
		"language": "html",
		"label_field": "title",
		"icon": "▤",
		"creatable": True,
		"new_fields": [
			{"fieldname": "title", "label": "Title", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "route", "label": "Route", "fieldtype": "Data"},
		],
	},
	# Report's editable field is chosen per record (see resolve_field).
	"Report": {
		"edit": "field",
		"field": None,
		"language": None,
		"icon": "☰",
		"list_filters": {"report_type": ["Query Report", "Script Report"]},
		"creatable": True,
		"new_fields": [
			{"fieldname": "report_name", "label": "Report Name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "ref_doctype", "label": "Reference DocType", "fieldtype": "Link", "options": "DocType", "reqd": 1},
			{
				"fieldname": "report_type",
				"label": "Report Type",
				"fieldtype": "Select",
				"options": "Query Report\nScript Report",
				"default": "Query Report",
				"reqd": 1,
			},
		],
	},
	# ---- whole-document (JSON) objects ----------------------------------
	"DocType": {
		"edit": "json",
		"language": "json",
		"icon": "❏",
		"list_filters": {"custom": 1},  # only custom doctypes are DB-safe to edit
		"creatable": True,
		"new_fields": [
			{"fieldname": "name", "label": "DocType Name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "module", "label": "Module", "fieldtype": "Link", "options": "Module Def", "default": "Custom", "reqd": 1},
		],
	},
	"Page": {
		"edit": "json",
		"language": "json",
		"label_field": "title",
		"icon": "🗎",
		"list_filters": {"standard": "No"},  # DB-stored custom pages only
		"creatable": False,  # a Desk Page needs JS files — create in Mode A
		"new_fields": [],
	},
	"Custom Field": {
		"edit": "json",
		"language": "json",
		"label_field": "label",
		"icon": "＋",
		"creatable": True,
		"new_fields": [
			{"fieldname": "dt", "label": "DocType", "fieldtype": "Link", "options": "DocType", "reqd": 1},
			{"fieldname": "label", "label": "Field Label", "fieldtype": "Data", "reqd": 1},
			{
				"fieldname": "fieldtype",
				"label": "Field Type",
				"fieldtype": "Select",
				"options": "Data\nInt\nFloat\nCurrency\nCheck\nSelect\nLink\nDate\nDatetime\nText\nSmall Text\nLong Text\nText Editor\nCode\nTable",
				"default": "Data",
				"reqd": 1,
			},
			{"fieldname": "insert_after", "label": "Insert After (fieldname)", "fieldtype": "Data"},
		],
	},
	"Property Setter": {
		"edit": "json",
		"language": "json",
		"icon": "⚙",
		"creatable": True,
		"new_fields": [
			{"fieldname": "doc_type", "label": "DocType", "fieldtype": "Link", "options": "DocType", "reqd": 1},
			{
				"fieldname": "doctype_or_field",
				"label": "Applies To",
				"fieldtype": "Select",
				"options": "DocField\nDocType",
				"default": "DocField",
				"reqd": 1,
			},
			{"fieldname": "field_name", "label": "Field Name", "fieldtype": "Data"},
			{"fieldname": "property", "label": "Property", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "value", "label": "Value", "fieldtype": "Data"},
			{
				"fieldname": "property_type",
				"label": "Property Type",
				"fieldtype": "Select",
				"options": "Data\nCheck\nInt\nSelect\nText",
				"default": "Data",
			},
		],
	},
}


def get_config(object_type: str) -> dict:
	"""Return the registry entry for ``object_type`` or raise.

	This is the guard that confines Frappe VS to known, framework-safe objects.
	"""
	cfg = OBJECT_TYPES.get(object_type)
	if not cfg:
		frappe.throw(
			_("{0} is not an editable object type in Frappe VS").format(object_type),
			frappe.PermissionError,
		)
	return cfg


def resolve_field(object_type: str, doc) -> tuple[str, str]:
	"""Resolve (fieldname, monaco_language) for a *field*-mode object.

	Handles ``Report``, whose editable field depends on its ``report_type``.
	"""
	cfg = get_config(object_type)
	if object_type == "Report":
		if (doc.get("report_type") if hasattr(doc, "get") else None) == "Script Report":
			return "report_script", "python"
		return "query", "sql"
	return cfg["field"], cfg["language"]


# --------------------------------------------------------------------------- #
# Scaffolding templates for "New …"
# --------------------------------------------------------------------------- #
def scaffold(object_type: str, values: dict) -> dict:
	"""Build a minimal, valid new-document dict from a template + user values.

	The returned dict is inserted via the DocType API, so any missing/invalid
	value is caught by the doctype's own validation and surfaced to the user.
	"""
	get_config(object_type)
	v = values or {}

	if object_type == "Server Script":
		st = v.get("script_type") or "API"
		doc = {
			"doctype": "Server Script",
			"name": v.get("name"),
			"script_type": st,
			"script": "# New Server Script\n",
		}
		if st == "API":
			doc["api_method"] = frappe.scrub(v.get("name") or "")
			doc["script"] = "frappe.response['message'] = 'Hello from Frappe VS'\n"
		elif st == "Scheduler Event":
			doc["event_frequency"] = "Daily"
			doc["script"] = "# runs on schedule\n"
		elif st == "DocType Event":
			doc["reference_doctype"] = v.get("reference_doctype")
			doc["doctype_event"] = "After Insert"
			doc["script"] = "# doc is available here\n# doc.flags ...\n"
		elif st == "Permission Query":
			doc["reference_doctype"] = v.get("reference_doctype")
			doc["script"] = "conditions = ''\n"
		return doc

	if object_type == "Client Script":
		return {
			"doctype": "Client Script",
			"name": v.get("name"),
			"dt": v.get("dt"),
			"view": v.get("view") or "Form",
			"enabled": 1,
			"script": (
				"frappe.ui.form.on('%s', {\n"
				"    refresh(frm) {\n"
				"        // your code here\n"
				"    },\n"
				"});\n" % (v.get("dt") or "DocType")
			),
		}

	if object_type == "Print Format":
		return {
			"doctype": "Print Format",
			"name": v.get("name"),
			"doc_type": v.get("doc_type"),
			"standard": "No",
			"custom_format": 1,
			"print_format_type": "Jinja",
			"html": "<div class=\"print-format\">\n    <h2>{{ doc.name }}</h2>\n</div>\n",
		}

	if object_type == "Web Template":
		return {
			"doctype": "Web Template",
			"name": v.get("name"),
			"type": v.get("type") or "Component",
			"standard": 0,
			"template": "<div>\n    <!-- {{ }} Jinja context available -->\n</div>\n",
		}

	if object_type == "Notification":
		return {
			"doctype": "Notification",
			"name": v.get("subject"),
			"subject": v.get("subject"),
			"document_type": v.get("document_type"),
			"event": v.get("event") or "New",
			"channel": v.get("channel") or "Email",
			"enabled": 0,
			"message": "Hello,\n\n{{ doc.name }} was updated.\n",
		}

	if object_type == "Web Page":
		return {
			"doctype": "Web Page",
			"title": v.get("title"),
			"route": v.get("route") or frappe.scrub(v.get("title") or "").replace("_", "-"),
			"content_type": "HTML",
			"published": 0,
			"main_section_html": "<section>\n    <h1>{{ title }}</h1>\n</section>\n",
		}

	if object_type == "Report":
		rt = v.get("report_type") or "Query Report"
		doc = {
			"doctype": "Report",
			"report_name": v.get("report_name"),
			"ref_doctype": v.get("ref_doctype"),
			"report_type": rt,
			"is_standard": "No",
		}
		if rt == "Script Report":
			doc["report_script"] = "result = []\ndata = result\n"
		else:
			doc["query"] = "select name from `tab%s` limit 20" % (v.get("ref_doctype") or "DocType")
		return doc

	if object_type == "DocType":
		return {
			"doctype": "DocType",
			"name": v.get("name"),
			"module": v.get("module") or "Custom",
			"custom": 1,
			"naming_rule": "Set by user",
			"autoname": "prompt",
			"fields": [
				{"fieldname": "title", "label": "Title", "fieldtype": "Data", "in_list_view": 1},
			],
			"permissions": [
				{
					"role": "System Manager",
					"read": 1,
					"write": 1,
					"create": 1,
					"delete": 1,
				}
			],
		}

	if object_type == "Custom Field":
		label = v.get("label") or ""
		return {
			"doctype": "Custom Field",
			"dt": v.get("dt"),
			"label": label,
			"fieldname": frappe.scrub(label),
			"fieldtype": v.get("fieldtype") or "Data",
			"insert_after": v.get("insert_after") or "",
		}

	if object_type == "Property Setter":
		return {
			"doctype": "Property Setter",
			"doc_type": v.get("doc_type"),
			"doctype_or_field": v.get("doctype_or_field") or "DocField",
			"field_name": v.get("field_name"),
			"property": v.get("property"),
			"value": v.get("value"),
			"property_type": v.get("property_type") or "Data",
		}

	frappe.throw(_("{0} cannot be created from Frappe VS").format(object_type))
