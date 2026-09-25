#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import re
import select
import subprocess
import sys
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

# knobs
model = "gemma4:12b"
api_base = "http://127.0.0.1:11434"
temperature = .3
top_p = 0.90
top_k = 40
context_window = 32768
max_tokens = 1024
repeat_penalty = 1
think = True
max_turns = 30
name = "bravectl"
user_name = "user"

system_message = """
you are bravectl, a browser control agent. You drive Brave over CDP.

After each tool result, stop and think: did url/title match what you wanted? Then either the next tool or a final answer.

- One tool per turn. goto has url only (optional tab).
- Tool output is url, title, and visible page text. Use that.
- Click a unique product or link name. Never click generic labels that several cards share: See all features, Apply now, Learn more, Read more.
- If the caption url/title is not what you wanted, do not click the same label. Click a unique name, or goto a url that already appeared in the caption. Never guess a path.
- If the caption repeats twice, stop. Name the matching product from what you have. Do not apply unless the user asked to apply.
- Never write a report or ask what they meant.
- scroll("down") / scroll("up") moves the pane that belongs to the last click (a dropdown, filter list, or page list). Click that control first, then scroll. With no prior click it scrolls the largest list on the page. Caption is viewport text; if it does not change, you are not on the pane you wanted — click the list, then scroll again. Do not spam scroll.

# tools
- launch(url)
- new_tab(url)
- goto(url)
- click(query) — visible text, CSS, or XPath. Unique label only.
- scroll(direction) — "down" or "up". Last-clicked pane; else the largest list.
- type_text(text) — replace=true clears first
- key(combo)
- screenshot()
- switch_tab(tab)
- stop()
"""

messages = []

import main as bravectl
from toolbox import cdp as _cdp

_TAB = {"type": "string", "description": "tab id, title, or url substring; omit for current"}


def _fn(name, description, properties, required=None):
	schema = {
		"type": "object",
		"properties": properties,
	}
	if required:
		schema["required"] = required
	return {
		"type": "function",
		"function": {"name": name, "description": description, "parameters": schema},
	}


def _captioned(fn):
	def inner(**kwargs):
		try:
			out = fn(**kwargs)
		except Exception as exc:
			return "ERROR: %s" % exc
		tab = kwargs.get("tab") or ""
		parts = []
		if isinstance(out, str) and not out.endswith(".png"):
			parts.append(out)
		elif out is not None and not isinstance(out, str):
			parts.append(json.dumps(out, default=str))
		try:
			parts.append(_cdp.caption(tab))
		except Exception as exc:
			parts.append("caption failed: %s" % exc)
		return "\n".join(p for p in parts if p) or "(ok)"

	return inner


def _plain(fn):
	def inner(**kwargs):
		try:
			out = fn(**kwargs)
		except Exception as exc:
			return "ERROR: %s" % exc
		if out is None:
			return "(ok)"
		if isinstance(out, str):
			return out
		return json.dumps(out, default=str)

	return inner


TOOL_HANDLERS = {
	"launch": _captioned(bravectl.launch),
	"new_tab": _captioned(bravectl.new_tab),
	"stop": _plain(bravectl.stop),
	"switch_tab": _captioned(bravectl.switch_tab),
	"goto": _captioned(bravectl.goto),
	"screenshot": _captioned(bravectl.screenshot),
	"click": _captioned(bravectl.click),
	"type_text": _captioned(bravectl.type_text),
	"key": _captioned(bravectl.key),
	"scroll": _captioned(bravectl.scroll),
}

TOOLS = [
	_fn("launch", "Start Brave or open a tab.", {"url": {"type": "string"}}, ["url"]),
	_fn("new_tab", "Open a new tab.", {"url": {"type": "string"}}, ["url"]),
	_fn("stop", "Quit the browser.", {}, None),
	_fn("switch_tab", "Make a tab current.", {"tab": {"type": "string"}}, ["tab"]),
	_fn("goto", "Navigate the current tab.", {"url": {"type": "string"}, "tab": _TAB}, ["url"]),
	_fn("screenshot", "Viewport screenshot.", {"tab": _TAB}, None),
	_fn("click", "Click a unique visible label, CSS, or XPath. Do not use generic CTAs.", {"query": {"type": "string"}, "tab": _TAB, "screenshot": {"type": "boolean"}}, ["query"]),
	_fn("type_text", "Type into the focused field.", {"text": {"type": "string"}, "tab": _TAB, "replace": {"type": "boolean"}}, ["text"]),
	_fn("key", "Press a key or chord.", {"combo": {"type": "string"}, "tab": _TAB}, ["combo"]),
	_fn("scroll", "Scroll down or up. Moves the last-clicked overflow pane; with no click, the largest list.", {"direction": {"type": "string", "description": "down or up"}, "tab": _TAB}, ["direction"]),
]


def _base():
	raw = (api_base or "http://127.0.0.1:11434").rstrip("/")
	if not raw.startswith("http"):
		raw = "http://" + raw
	if raw.endswith("/v1"):
		raw = raw[: -len("/v1")]
	return raw


def _options():
	opt = {}
	if temperature is not None:
		opt["temperature"] = temperature
	if top_p is not None:
		opt["top_p"] = top_p
	if top_k is not None:
		opt["top_k"] = top_k
	if context_window is not None:
		opt["num_ctx"] = context_window
	if max_tokens is not None:
		opt["num_predict"] = max_tokens
	if repeat_penalty is not None:
		opt["repeat_penalty"] = repeat_penalty
	return opt


class EscapeWatch:
	def __init__(self, on_escape):
		self.on_escape = on_escape
		self._stop = threading.Event()
		self._fd = None
		self._old = None
		self._thread = None

	def __enter__(self):
		try:
			import termios
			import tty
		except ImportError:
			return self
		if not sys.stdin.isatty():
			return self
		self._fd = sys.stdin.fileno()
		try:
			self._old = termios.tcgetattr(self._fd)
			tty.setcbreak(self._fd)
		except termios.error:
			self._old = None
			return self
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()
		return self

	def __exit__(self, *exc):
		self._stop.set()
		if self._old is not None and self._fd is not None:
			try:
				import termios
				termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
			except Exception:
				pass
		if self._thread is not None:
			self._thread.join(timeout=0.2)
		return False

	def _run(self):
		fd = self._fd
		while not self._stop.is_set():
			try:
				ready, _, _ = select.select([fd], [], [], 0.05)
			except (OSError, ValueError):
				break
			if not ready:
				continue
			try:
				ch = os.read(fd, 1)
			except OSError:
				break
			if ch != b"\x1b":
				continue
			try:
				more, _, _ = select.select([fd], [], [], 0.03)
			except (OSError, ValueError):
				more = []
			if more:
				try:
					os.read(fd, 32)
				except OSError:
					pass
				continue
			self._stop.set()
			try:
				self.on_escape()
			except Exception:
				pass
			return


RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
GRAY = "\033[90m"


def _c(text, *codes):
	if not sys.stdout.isatty():
		return text
	return "".join(codes) + text + RESET


def _http_json(path, payload=None, timeout=5):
	url = _base() + path
	data = None
	headers = {}
	if payload is not None:
		data = json.dumps(payload).encode()
		headers["Content-Type"] = "application/json"
	req = urllib.request.Request(url, data=data, headers=headers)
	with urllib.request.urlopen(req, timeout=timeout) as resp:
		return json.loads(resp.read().decode())


def list_models():
	data = _http_json("/api/tags")
	return [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]


def format_params():
	names = ",".join(TOOL_HANDLERS)
	return (
		"temp %s  top_p %s  top_k %s  ctx %s  max %s  rep %s  think %s  tools %s"
		% (temperature, top_p, top_k, context_window, max_tokens, repeat_penalty, think, names)
	)


def _stream_chat(payload):
	url = _base() + "/api/chat"
	data = json.dumps(payload).encode()
	req = urllib.request.Request(
		url, data=data, headers={"Content-Type": "application/json"}
	)
	done = {
		"message": {"role": "assistant", "content": ""},
		"usage": {},
		"error": None,
		"interrupted": False,
	}
	resp = None
	try:
		resp = urllib.request.urlopen(req, timeout=None)
	except urllib.error.HTTPError as e:
		detail = e.read().decode(errors="replace")
		try:
			detail = json.loads(detail).get("error") or detail
		except ValueError:
			pass
		done["error"] = "ollama: %s" % (detail or e.code)
		yield {"_result": True, **done}
		return
	except urllib.error.URLError as e:
		done["error"] = "ollama: %s" % e
		yield {"_result": True, **done}
		return

	content_parts = []
	thinking_parts = []
	tool_calls = None
	usage = {}
	interrupted = False

	def _cancel():
		nonlocal interrupted
		interrupted = True
		try:
			resp.close()
		except Exception:
			pass

	try:
		with EscapeWatch(_cancel):
			buf = b""
			while not interrupted:
				chunk = resp.read(256)
				if not chunk:
					break
				buf += chunk
				while b"\n" in buf:
					line, buf = buf.split(b"\n", 1)
					line = line.strip()
					if not line:
						continue
					try:
						obj = json.loads(line)
					except json.JSONDecodeError:
						continue
					if obj.get("error"):
						done["error"] = str(obj["error"])
						interrupted = True
						break
					msg = obj.get("message") or {}
					thinking = msg.get("thinking") or ""
					if thinking:
						thinking_parts.append(thinking)
						yield {"type": "thought", "data": thinking}
					text = msg.get("content") or ""
					if text:
						content_parts.append(text)
						yield {"type": "text", "data": text}
					if msg.get("tool_calls"):
						tool_calls = msg["tool_calls"]
					if obj.get("done"):
						usage = {
							"input_tokens": int(obj.get("prompt_eval_count") or 0),
							"output_tokens": int(obj.get("eval_count") or 0),
						}
						break
	finally:
		try:
			resp.close()
		except Exception:
			pass

	final = {"role": "assistant", "content": "".join(content_parts)}
	if thinking_parts:
		final["thinking"] = "".join(thinking_parts)
	if tool_calls:
		final["tool_calls"] = tool_calls
	done["interrupted"] = interrupted
	done["message"] = final
	done["usage"] = usage
	yield {"_result": True, **done}


def _call_tool(call):
	fn = call.get("function") or call
	name = fn.get("name") or "tool"
	raw = fn.get("arguments") or {}
	if isinstance(raw, str):
		try:
			args = json.loads(raw) if raw else {}
		except json.JSONDecodeError:
			args = {}
	else:
		args = raw if isinstance(raw, dict) else {}
	args = {k: v for k, v in args.items() if v is not None}
	preview = ""
	if args:
		first = next(iter(args.values()), "")
		preview = "  " + str(first).replace("\n", " ")[:80]
	yield {"type": "tool_call", "title": "%s%s" % (name, preview)}
	handler = TOOL_HANDLERS.get(name)
	if handler is None:
		text = "ERROR: unknown tool %s. Available: %s" % (name, ", ".join(TOOL_HANDLERS))
		yield {"type": "tool_call_update", "status": "failed"}
		messages.append({"role": "tool", "tool_name": name, "content": text})
		return
	try:
		text = handler(**args)
		status = "failed" if str(text).startswith("ERROR:") else "completed"
	except Exception as exc:
		text = "ERROR: %s" % exc
		status = "failed"
	messages.append({"role": "tool", "tool_name": name, "content": str(text)})
	yield {"type": "tool_call_update", "status": status, "preview": str(text)[:1200]}


def turn(prompt):
	global messages
	if not messages:
		text = (system_message or "").strip()
		if text:
			messages.append({"role": "system", "content": text})
	messages.append({"role": "user", "content": prompt})
	usage_total = {"input_tokens": 0, "output_tokens": 0}
	first = True
	for round_i in range(max(1, int(max_turns))):
		payload = {
			"model": model,
			"messages": messages,
			"stream": True,
			"options": _options(),
			"tools": TOOLS,
		}
		if think is not None:
			payload["think"] = think
		result = None
		for item in _stream_chat(payload):
			if item.get("_result"):
				result = item
				break
			yield item
		if result["error"]:
			if first and messages and messages[-1].get("role") == "user":
				messages.pop()
			yield {"type": "error", "message": result["error"]}
			return
		if result["interrupted"]:
			if first and messages and messages[-1].get("role") == "user":
				messages.pop()
			raise KeyboardInterrupt
		first = False
		assistant = result["message"]
		if assistant.get("content") or assistant.get("thinking") or assistant.get("tool_calls"):
			messages.append(assistant)
		elif messages and messages[-1].get("role") == "user" and round_i == 0:
			messages.pop()
		u = result.get("usage") or {}
		usage_total["input_tokens"] += int(u.get("input_tokens") or 0)
		usage_total["output_tokens"] += int(u.get("output_tokens") or 0)
		calls = assistant.get("tool_calls") or []
		if not calls:
			break
		for call in calls:
			yield from _call_tool(call)
		if round_i == int(max_turns) - 1:
			yield {"type": "error", "message": "hit max_turns (%s) with pending tools" % max_turns}
			break
	if usage_total["input_tokens"] or usage_total["output_tokens"]:
		yield {"type": "usage", "usage": usage_total}


def render(prompt):
	stage = None
	usage = {}
	ok = True
	for event in turn(prompt):
		kind = event.get("type")
		if kind == "thought":
			chunk = event.get("data") or ""
			if not chunk:
				continue
			if stage != "thought":
				print(_c("\nThinking: ", RED, BOLD), end="", flush=True)
				stage = "thought"
			print(_c(chunk, RED), end="", flush=True)
		elif kind == "text":
			chunk = event.get("data") or ""
			if not chunk:
				continue
			if stage != "text":
				print(_c("\n%s: " % name, BOLD, GREEN), end="", flush=True)
				stage = "text"
			print(_c(chunk, GREEN), end="", flush=True)
		elif kind == "tool_call":
			print(_c("\n  ↳ %s" % (event.get("title") or "tool"), YELLOW), flush=True)
			stage = "tool"
		elif kind == "tool_call_update":
			status = event.get("status")
			if status == "completed":
				print(_c("    done", GRAY), flush=True)
			elif status == "failed":
				print(_c("    failed", RED), flush=True)
			preview = (event.get("preview") or "").strip()
			if preview:
				print(_c(preview, GRAY), flush=True)
			stage = "tool"
		elif kind == "error":
			print(_c("\nERROR: %s" % event.get("message", event), RED, BOLD))
			stage = "error"
			ok = False
		elif kind == "usage":
			usage = event.get("usage") or {}
	if stage is not None:
		print()
	if usage:
		print(
			_c(
				"  tokens  %s in · %s out"
				% (usage.get("input_tokens") or 0, usage.get("output_tokens") or 0),
				YELLOW,
			)
		)
	if messages:
		last = messages[-1]
		if last.get("role") == "assistant" and last.get("tool_calls"):
			ok = False
	save_run(prompt, ok)


def _slug(text):
	s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
	return (s[:48] or "run").rstrip("-")


def save_run(prompt, ok, push=None):
	os.makedirs(dataset_dir, exist_ok=True)
	stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
	rid = "%s-%s" % (stamp, _slug(prompt))
	path = os.path.join(dataset_dir, rid + ".jsonl")
	row = {"id": rid, "ok": bool(ok), "model": model, "messages": list(messages)}
	with open(path, "w") as f:
		f.write(json.dumps(row, ensure_ascii=False) + "\n")
	print(_c("  saved  %s  ok=%s" % (path, ok), GRAY))
	if push is None:
		push = push_remote
	if push:
		push_run(path)


def push_run(path):
	cmd = [
		"rsync",
		"-e",
		"ssh -p %s -i %s -o BatchMode=yes" % (remote_port, remote_key),
		path,
		"%s:%s" % (remote_host, remote_inbox),
	]
	try:
		subprocess.run(cmd, check=True, capture_output=True, text=True)
		print(_c("  pushed %s" % os.path.basename(path), GRAY))
	except FileNotFoundError:
		print(_c("  rsync not found; left %s" % path, YELLOW))
	except subprocess.CalledProcessError as e:
		err = (e.stderr or e.stdout or str(e)).strip()
		print(_c("  push failed: %s" % err, YELLOW))


def _boxed():
	try:
		from prompt_toolkit import Application
		from prompt_toolkit.history import FileHistory
		from prompt_toolkit.key_binding import KeyBindings
		from prompt_toolkit.layout import Layout
		from prompt_toolkit.layout.containers import HSplit
		from prompt_toolkit.widgets import Frame, TextArea
	except ImportError:
		return None
	if not (sys.stdin.isatty() and sys.stdout.isatty()):
		return None

	def ask():
		hist = FileHistory(os.path.join(ROOT, ".bravectl_ptk_history"))
		area = TextArea(multiline=True, wrap_lines=True, history=hist)
		kb = KeyBindings()

		@kb.add("enter")
		def _submit(event):
			event.app.exit(result=area.text)

		@kb.add("c-j")
		def _nl(event):
			area.buffer.insert_text("\n")

		@kb.add("c-c")
		def _int(event):
			event.app.exit(exception=KeyboardInterrupt)

		@kb.add("escape", eager=True)
		def _esc(event):
			event.app.exit(exception=KeyboardInterrupt)

		@kb.add("c-d")
		def _eof(event):
			event.app.exit(exception=EOFError)

		frame = Frame(
			area,
			title="%s | Enter send | Ctrl+J newline | Esc cancel | Ctrl+D quit"
			% user_name,
			style="fg:ansigreen bold",
		)
		app = Application(
			layout=Layout(HSplit([frame])),
			key_bindings=kb,
			full_screen=False,
			mouse_support=False,
		)
		return app.run()

	return ask


def handle_slash(text):
	if not text.startswith("/"):
		return False
	parts = text.split()
	cmd = parts[0].lower()
	arg = " ".join(parts[1:]).strip()
	global model, api_base, temperature, top_p, top_k, context_window, max_tokens, repeat_penalty, think
	if cmd in {"/quit", "/exit", "/q"}:
		raise EOFError
	if cmd == "/model":
		if arg:
			model = arg
		print(_c("  ollama · %s  ·  %s" % (model, _base()), CYAN))
		try:
			print(_c("  models: " + ", ".join(list_models()), GRAY))
		except Exception as e:
			print(_c("  models: %s" % e, YELLOW))
		return True
	if cmd == "/api":
		if arg:
			api_base = arg
		print(_c("  api  %s" % _base(), CYAN))
		return True
	if cmd == "/params":
		if arg:
			bits = arg.split(None, 1)
			key = bits[0].lower()
			raw = bits[1].strip() if len(bits) > 1 else ""
			if key in {"temp", "temperature"} and raw:
				temperature = float(raw)
			elif key == "top_p" and raw:
				top_p = float(raw)
			elif key == "top_k" and raw:
				if raw.lower() in {"none", "default", "-"}:
					top_k = None
				else:
					top_k = int(raw)
			elif key in {"ctx", "context"} and raw:
				context_window = int(raw)
			elif key == "max" and raw:
				max_tokens = int(raw)
			elif key in {"rep", "repeat"} and raw:
				repeat_penalty = float(raw)
			elif key == "think" and raw:
				think = raw.lower() in {"on", "true", "1", "yes"}
		print(_c("  %s" % format_params(), CYAN))
		return True
	return False


def repl():
	print(_c(name, BOLD, CYAN) + "  (exit / quit)")
	print(_c("  ollama · %s  ·  %s" % (model, _base()), GRAY))
	print(_c("  %s" % format_params(), GRAY))
	boxed = _boxed()
	while True:
		try:
			if boxed:
				user = (boxed() or "").strip()
			else:
				user = input("\n%s: " % user_name).strip()
		except EOFError:
			print()
			break
		except KeyboardInterrupt:
			continue
		if not user:
			continue
		if user.lower() in {"exit", "quit"}:
			break
		if handle_slash(user):
			continue
		try:
			render(user)
		except KeyboardInterrupt:
			print(_c("\n(interrupted)", YELLOW))
		except Exception as e:
			print(_c("ERROR: %s" % e, RED, BOLD))


def main():
	parser = argparse.ArgumentParser(description="bravectl ollama chat")
	parser.add_argument("-c", dest="cmd", default=None, help="one shot prompt, then exit")
	parser.add_argument("--no-push", action="store_true", help="write jsonl but do not rsync")
	args = parser.parse_args()
	global push_remote
	if args.no_push:
		push_remote = False
	if args.cmd:
		render(args.cmd)
		return
	repl()


if __name__ == "__main__":
	main()
