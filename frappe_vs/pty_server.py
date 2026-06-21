"""Standalone PTY-over-WebSocket server for Frappe VS Mode A (terminal).

This is launched on demand by ``frappe_vs.api.terminal_start`` (which is
developer_mode + System-Manager gated). It is deliberately decoupled from
Frappe: pure stdlib + redis-py, so it has no Frappe request lifecycle to fight.

Security model:
  * Binds **127.0.0.1 only** — never exposed to the network.
  * Each WebSocket connection must present a **single-use token** (issued by the
    gated endpoint and stored in redis with a short TTL); the token is consumed
    on connect.
  * It then forks a real login shell on a PTY in the bench directory with the
    bench virtualenv on PATH. That is full shell access (RCE) by design — it is
    only ever reachable in developer_mode by a System Manager on localhost.

Run directly:
    python -m frappe_vs.pty_server --bench-root <path> --redis-url <url> \
        --host 127.0.0.1 --port 7900
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import socket
import struct
import termios
import threading

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# WebSocket opcodes
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 framing
# --------------------------------------------------------------------------- #
def ws_accept_key(key: str) -> str:
	digest = hashlib.sha1((key + WS_GUID).encode()).digest()
	return base64.b64encode(digest).decode()


def encode_frame(opcode: int, payload: bytes) -> bytes:
	"""Server -> client frame (never masked)."""
	header = bytearray([0x80 | opcode])
	n = len(payload)
	if n < 126:
		header.append(n)
	elif n < 65536:
		header.append(126)
		header += struct.pack(">H", n)
	else:
		header.append(127)
		header += struct.pack(">Q", n)
	return bytes(header) + payload


def parse_frame(buf: bytearray):
	"""Parse one client frame from ``buf``. Returns (consumed, opcode, payload)
	or None if more bytes are needed. Client frames are masked."""
	if len(buf) < 2:
		return None
	b0, b1 = buf[0], buf[1]
	opcode = b0 & 0x0F
	masked = b1 & 0x80
	length = b1 & 0x7F
	idx = 2
	if length == 126:
		if len(buf) < idx + 2:
			return None
		length = struct.unpack(">H", buf[idx : idx + 2])[0]
		idx += 2
	elif length == 127:
		if len(buf) < idx + 8:
			return None
		length = struct.unpack(">Q", buf[idx : idx + 8])[0]
		idx += 8
	mask = b""
	if masked:
		if len(buf) < idx + 4:
			return None
		mask = buf[idx : idx + 4]
		idx += 4
	if len(buf) < idx + length:
		return None
	payload = bytearray(buf[idx : idx + length])
	if masked:
		for i in range(length):
			payload[i] ^= mask[i % 4]
	return idx + length, opcode, bytes(payload)


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #
class PtyServer:
	def __init__(self, bench_root: str, redis_url: str, host: str, port: int):
		self.bench_root = os.path.realpath(bench_root)
		self.redis_url = redis_url
		self.host = host
		self.port = port

	def _redis(self):
		import redis  # bundled with frappe

		return redis.from_url(self.redis_url)

	def _consume_token(self, token: str):
		"""Return the user the token was issued to, deleting it (single use)."""
		if not token:
			return None
		key = f"fvs_term_token:{token}"
		r = self._redis()
		pipe = r.pipeline()
		pipe.get(key)
		pipe.delete(key)
		value, _ = pipe.execute()
		if value is None:
			return None
		return value.decode() if isinstance(value, bytes) else value

	def serve(self):
		signal.signal(signal.SIGCHLD, signal.SIG_IGN)
		srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		srv.bind((self.host, self.port))
		srv.listen(8)
		print(f"[fvs-pty] listening on {self.host}:{self.port} (bench={self.bench_root})", flush=True)
		while True:
			try:
				conn, _addr = srv.accept()
			except OSError:
				continue
			threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

	# -- per connection ----------------------------------------------------- #
	def _handle(self, conn: socket.socket):
		try:
			request = self._read_http_request(conn)
			if request is None:
				conn.close()
				return
			headers, path = request
			key = headers.get("sec-websocket-key")
			if not key:
				conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
				conn.close()
				return

			token = self._token_from_path(path)
			user = self._consume_token(token)
			if not user:
				# Complete the handshake just enough to send a clean close reason.
				self._do_handshake(conn, key)
				conn.sendall(encode_frame(OP_CLOSE, b"\x03\xe8unauthorized"))
				conn.close()
				return

			self._do_handshake(conn, key)
			self._bridge(conn)
		except Exception as e:  # noqa: BLE001
			print(f"[fvs-pty] connection error: {e}", flush=True)
			try:
				conn.close()
			except OSError:
				pass

	def _read_http_request(self, conn: socket.socket):
		conn.settimeout(10)
		buf = b""
		while b"\r\n\r\n" not in buf:
			chunk = conn.recv(1024)
			if not chunk:
				return None
			buf += chunk
			if len(buf) > 16384:
				return None
		conn.settimeout(None)
		lines = buf.decode("latin-1").split("\r\n")
		request_line = lines[0].split(" ")
		path = request_line[1] if len(request_line) > 1 else "/"
		headers = {}
		for line in lines[1:]:
			if ":" in line:
				k, v = line.split(":", 1)
				headers[k.strip().lower()] = v.strip()
		return headers, path

	def _token_from_path(self, path: str) -> str:
		if "?" not in path:
			return ""
		query = path.split("?", 1)[1]
		for part in query.split("&"):
			if part.startswith("token="):
				return part[len("token=") :]
		return ""

	def _do_handshake(self, conn: socket.socket, key: str):
		accept = ws_accept_key(key)
		conn.sendall(
			(
				"HTTP/1.1 101 Switching Protocols\r\n"
				"Upgrade: websocket\r\n"
				"Connection: Upgrade\r\n"
				f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
			).encode()
		)

	def _bridge(self, conn: socket.socket):
		master, slave = pty.openpty()
		pid = os.fork()
		if pid == 0:
			# -- child: become the shell on the slave PTY -------------------- #
			try:
				os.close(master)
				os.setsid()
				fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
				os.dup2(slave, 0)
				os.dup2(slave, 1)
				os.dup2(slave, 2)
				if slave > 2:
					os.close(slave)
				env = dict(os.environ)
				venv = os.path.join(self.bench_root, "env")
				env["PATH"] = os.path.join(venv, "bin") + os.pathsep + env.get("PATH", "")
				env["VIRTUAL_ENV"] = venv
				env["TERM"] = "xterm-256color"
				env["PYTHONUNBUFFERED"] = "1"
				os.chdir(self.bench_root)
				shell = env.get("SHELL") or "/bin/bash"
				os.execvpe(shell, [shell, "-i"], env)
			except Exception:
				os._exit(1)

		# -- parent: bridge master fd <-> websocket ------------------------- #
		os.close(slave)
		buf = bytearray()
		try:
			while True:
				rlist, _, _ = select.select([conn, master], [], [])
				if master in rlist:
					try:
						data = os.read(master, 65536)
					except OSError:
						data = b""
					if not data:
						break
					conn.sendall(encode_frame(OP_BIN, data))
				if conn in rlist:
					try:
						chunk = conn.recv(65536)
					except OSError:
						chunk = b""
					if not chunk:
						break
					buf += chunk
					if not self._drain_ws(conn, buf, master):
						break
		finally:
			try:
				os.close(master)
			except OSError:
				pass
			try:
				os.kill(pid, signal.SIGKILL)
			except OSError:
				pass
			try:
				conn.close()
			except OSError:
				pass

	def _drain_ws(self, conn, buf: bytearray, master: int) -> bool:
		"""Process complete frames in ``buf``. Returns False to close."""
		while True:
			parsed = parse_frame(buf)
			if parsed is None:
				return True
			consumed, opcode, payload = parsed
			del buf[:consumed]
			if opcode in (OP_TEXT,):
				# Control channel (resize), JSON: {"resize": [cols, rows]}
				try:
					msg = json.loads(payload.decode("utf-8"))
				except Exception:
					continue
				if isinstance(msg, dict) and "resize" in msg:
					cols, rows = msg["resize"]
					winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
					try:
						fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
					except OSError:
						pass
			elif opcode in (OP_BIN, OP_CONT):
				os.write(master, payload)
			elif opcode == OP_PING:
				conn.sendall(encode_frame(OP_PONG, payload))
			elif opcode == OP_CLOSE:
				return False
		return True


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--bench-root", required=True)
	parser.add_argument("--redis-url", required=True)
	parser.add_argument("--host", default="127.0.0.1")
	parser.add_argument("--port", type=int, default=7900)
	args = parser.parse_args()
	PtyServer(args.bench_root, args.redis_url, args.host, args.port).serve()


if __name__ == "__main__":
	main()
