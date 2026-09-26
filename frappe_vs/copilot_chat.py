"""The chat that composes change sets.

A consultant says what they want in a sentence; the model uses the builders in
``copilot`` to turn it into changes, and then writes them down as a VS Change
Set. Nothing reaches the site from here: the set is a proposal until someone
presses Apply, and Apply can be undone.

The provider is configured in the bench's site config, so one setting serves
every app on the bench::

    "ai_provider": "OpenAI compatible",   # or "Anthropic"
    "ai_base_url": "http://localhost:11434/v1",
    "ai_model": "llama3.2:3b",
    "ai_api_key": "…"                     # a local model needs none
"""

from __future__ import annotations

import json

import frappe
import requests
from frappe import _

from frappe_vs import copilot

MAX_ROUNDS = 8
TIMEOUT = 120

SYSTEM_PROMPT = """You are the Frappe VS copilot. You change an ERPNext site for a consultant, by proposing changes they then apply.

How to work:
- Look before you change: describe_doctype tells you what fields a form already has, and what to put a new field after.
- Build the change with add_field, set_property or create_report. Each call adds one change to the set you are composing; it does not touch the site.
- When the set is complete, call propose with a short title. The consultant reviews it and presses Apply. Say in one sentence what you proposed.
- You can only change fields, form properties and reports. Anything else — invoices, stock, customers, users — is out of reach, and you should say so plainly rather than pretend.
- If the request is unclear, ask one question instead of guessing. A wrong change on a live site costs the consultant time.

Keep replies to two or three sentences."""

TOOLS = [
	{
		"name": "describe_doctype",
		"description": "The fields a form already has, so a new field can be placed and an existing one referred to.",
		"parameters": {
			"type": "object",
			"properties": {"doctype": {"type": "string"}},
			"required": ["doctype"],
		},
	},
	{
		"name": "add_field",
		"description": "Add a field to a form. Adds one change to the set being composed.",
		"parameters": {
			"type": "object",
			"properties": {
				"doctype": {"type": "string"},
				"label": {"type": "string", "description": "The label on the form, for example: PO Number"},
				"fieldtype": {"type": "string", "description": "Data, Int, Currency, Date, Check, Select, Link, Text…"},
				"options": {
					"type": "string",
					"description": "For Link, the doctype it points at. For Select, the choices, one per line.",
				},
				"insert_after": {"type": "string", "description": "An existing fieldname."},
				"reqd": {"type": "boolean"},
			},
			"required": ["doctype", "label"],
		},
	},
	{
		"name": "set_property",
		"description": "Change one thing about an existing field: label, reqd, hidden, read_only, default.",
		"parameters": {
			"type": "object",
			"properties": {
				"doctype": {"type": "string"},
				"fieldname": {"type": "string"},
				"prop": {"type": "string"},
				"value": {"type": "string"},
			},
			"required": ["doctype", "fieldname", "prop", "value"],
		},
	},
	{
		"name": "create_report",
		"description": "A report from a single SELECT query.",
		"parameters": {
			"type": "object",
			"properties": {
				"title": {"type": "string"},
				"ref_doctype": {"type": "string"},
				"query": {"type": "string", "description": "A SELECT. Table names are like `tabSales Order`."},
			},
			"required": ["title", "ref_doctype", "query"],
		},
	},
	{
		"name": "propose",
		"description": "Write the composed changes down as a change set for the consultant to apply.",
		"parameters": {
			"type": "object",
			"properties": {"title": {"type": "string"}},
			"required": ["title"],
		},
	},
]


def config() -> dict:
	return {
		"provider": frappe.conf.get("ai_provider") or "OpenAI compatible",
		"base_url": frappe.conf.get("ai_base_url") or "",
		"model": frappe.conf.get("ai_model") or "",
		"api_key": frappe.conf.get("ai_api_key") or "",
	}


@frappe.whitelist()
def status() -> dict:
	settings = config()
	return {
		"on": bool(settings["model"] and (settings["base_url"] or settings["provider"] == "Anthropic")),
		"provider": settings["provider"],
		"model": settings["model"],
	}


@frappe.whitelist()
def chat(message: str, history: str | list | None = None) -> dict:
	"""One turn. Returns what to say, and the change set if one was proposed."""
	copilot._staff()
	if not status()["on"]:
		frappe.throw(_("No AI is configured on this bench yet."))

	messages = list(frappe.parse_json(history) or [])
	messages.append({"role": "user", "content": message})

	pending: list[dict] = []
	errors: list[str] = []
	request = message
	nudged = False
	for _round in range(MAX_ROUNDS):
		reply = _complete(messages)
		if not reply["tool_calls"] and not pending and not nudged:
			# A small model will sometimes describe the change instead of making
			# it. One nudge, then take it at its word.
			nudged = True
			messages.append(reply["raw"])
			messages.append(
				{
					"role": "user",
					"content": "Use the tools to make that change now. Do not describe it in words.",
				}
			)
			continue
		if not reply["tool_calls"]:
			# A model that built changes and then stopped talking still meant to
			# propose them; writing them down is what it was asked for.
			change_set = copilot.propose(_title(request), request, pending) if pending else None
			# A model will happily say it made a change that was refused. If
			# nothing came of the turn, the refusal is the honest answer.
			text = reply["text"]
			if not change_set and errors:
				text = _("I could not do that: {0}").format(errors[-1])
			return {"reply": text, "change_set": change_set, "pending": []}

		messages.append(reply["raw"])
		for call in reply["tool_calls"]:
			result, change_set = _run_tool(call["name"], call["arguments"], pending, request)
			if result.get("error"):
				errors.append(result["error"])
			messages.append(
				{
					"role": "tool",
					"tool_call_id": call["id"],
					"name": call["name"],
					"content": json.dumps(result, default=str)[:6000],
				}
			)
			if change_set:
				# One more round, so it can say what it did.
				closing = _complete(messages)
				return {
					"reply": closing["text"] or _("Proposed {0}.").format(change_set["title"]),
					"change_set": change_set,
					"pending": [],
				}

	return {"reply": _("I could not work that out. Could you say it more simply?"), "change_set": None}


def _title(request: str) -> str:
	"""A change set is named after what was asked for."""
	title = " ".join((request or "").split())[:80]
	return title or _("Change")


def _run_tool(name: str, arguments: dict, pending: list[dict], request: str):
	"""Builders add to the set being composed; propose writes it down."""
	try:
		if name == "describe_doctype":
			return _describe(arguments.get("doctype", "")), None

		if name == "add_field":
			change = copilot.add_field(
				doctype=arguments.get("doctype"),
				label=arguments.get("label"),
				fieldtype=arguments.get("fieldtype") or "Data",
				options=arguments.get("options"),
				insert_after=arguments.get("insert_after"),
				reqd=bool(arguments.get("reqd")),
			)
		elif name == "set_property":
			change = copilot.set_property(
				doctype=arguments.get("doctype"),
				fieldname=arguments.get("fieldname"),
				prop=arguments.get("prop"),
				value=arguments.get("value"),
			)
		elif name == "create_report":
			change = copilot.create_report(
				title=arguments.get("title"),
				ref_doctype=arguments.get("ref_doctype"),
				query=arguments.get("query"),
			)
		elif name == "propose":
			if not pending:
				return {"error": "There is nothing to propose yet."}, None
			change_set = copilot.propose(arguments.get("title") or _("Change"), request, pending)
			return {"proposed": change_set["name"]}, change_set
		else:
			return {"error": f"No such tool: {name}"}, None
	except frappe.ValidationError as e:
		# Hand the model its mistake, in its own words, so it can correct itself.
		return {"error": str(e)}, None

	pending.append(change)
	return {"added": change["summary"]}, None


def _describe(doctype: str) -> dict:
	if not frappe.db.exists("DocType", doctype):
		return {"error": f"There is no doctype called {doctype}."}
	meta = frappe.get_meta(doctype)
	return {
		"doctype": doctype,
		"fields": [
			{"fieldname": f.fieldname, "label": f.label, "fieldtype": f.fieldtype, "options": f.options}
			for f in meta.fields
			if f.fieldtype not in ("Section Break", "Column Break", "Tab Break", "HTML")
		][:120],
	}


def _complete(messages: list[dict]) -> dict:
	settings = config()
	if settings["provider"] == "Anthropic":
		return _anthropic(settings, messages)
	return _openai(settings, messages)


def _openai(settings: dict, messages: list[dict]) -> dict:
	body = {
		"model": settings["model"],
		"messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
		"tools": [{"type": "function", "function": t} for t in TOOLS],
		"temperature": 0,
	}
	data = _post(
		f"{settings['base_url'].rstrip('/')}/chat/completions",
		body,
		{"Authorization": f"Bearer {settings['api_key'] or 'none'}"},
	)
	choice = (data.get("choices") or [{}])[0].get("message") or {}
	return {
		"text": choice.get("content") or "",
		"raw": choice,
		"tool_calls": [
			{
				"id": c.get("id"),
				"name": (c.get("function") or {}).get("name"),
				"arguments": _loads((c.get("function") or {}).get("arguments")),
			}
			for c in choice.get("tool_calls") or []
		],
	}


def _anthropic(settings: dict, messages: list[dict]) -> dict:
	body = {
		"model": settings["model"],
		"max_tokens": 2048,
		"system": SYSTEM_PROMPT,
		"messages": _to_anthropic(messages),
		"tools": [
			{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
			for t in TOOLS
		],
	}
	url = f"{(settings['base_url'] or 'https://api.anthropic.com').rstrip('/')}/v1/messages"
	data = _post(url, body, {"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01"})
	blocks = data.get("content") or []
	return {
		"text": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
		"raw": {"role": "assistant", "content": blocks},
		"tool_calls": [
			{"id": b.get("id"), "name": b.get("name"), "arguments": b.get("input") or {}}
			for b in blocks
			if b.get("type") == "tool_use"
		],
	}


def _to_anthropic(messages: list[dict]) -> list[dict]:
	out = []
	for m in messages:
		if m.get("role") == "tool":
			out.append(
				{
					"role": "user",
					"content": [
						{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
					],
				}
			)
		else:
			out.append(m)
	return out


def _post(url: str, body: dict, headers: dict) -> dict:
	try:
		response = requests.post(
			url, json=body, headers={"Content-Type": "application/json", **headers}, timeout=TIMEOUT
		)
		response.raise_for_status()
		return response.json()
	except requests.RequestException as e:
		frappe.log_error(f"Frappe VS copilot: {e}", "Frappe VS copilot")
		frappe.throw(_("The AI could not be reached."))


def _loads(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	try:
		return json.loads(raw or "{}")
	except ValueError:
		return {}
