#!/usr/bin/env python3
"""Local OpenAI-compatible gateway for a Mistral Vibe subscription.

Bridges OpenAI-style /v1/chat/completions clients to Mistral using the key
the official Vibe CLI provisions for your plan, with an optional Mistral
AI Studio API key as pay-as-you-go fallback.

Key logic:
  - 429  -> retry the same key every RETRY_429_INTERVAL (10s) for
            RETRY_429_SECONDS (60s); then cooldown COOLDOWN_429 (60s) and
            the fallback key serves meanwhile
  - 401/402 -> cooldown COOLDOWN_DEAD (6h): budget spent or key revoked
  - 404  -> no cooldown, this request falls through to the fallback key
  - cooldowns persist in pool_state.json so restarts keep the state

Auth: clients send the gateway's own key from GATEWAY_KEY, or the gateway
      runs keyless on loopback.
"""
import datetime
import hashlib
import hmac
import http.client
import json
import logging
import os
import pathlib
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("mistral-gateway")

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
MISTRAL_WHOAMI_URL = "https://console.mistral.ai/api/vibe/whoami"

BASE_DIR = pathlib.Path(__file__).parent
STATE_FILE = BASE_DIR / "pool_state.json"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
POLL_SLEEP = 0.3
# Rejection code -> cooldown seconds for the account that was rejected.
# Built in main() after env files load, so gateway.env overrides apply.
COOLDOWNS: dict[int, float] = {}


def build_cooldowns() -> None:
    COOLDOWNS.update({
        429: float(os.environ.get("COOLDOWN_429", "60")),
        401: float(os.environ.get("COOLDOWN_DEAD", str(6 * 3600))),
        402: float(os.environ.get("COOLDOWN_DEAD", str(6 * 3600))),
    })


# 429 retry window ("seconds") and sleep between attempts ("interval"),
# built in main() after env files load like COOLDOWNS.
RETRY_429: dict[str, float] = {}


def build_retries() -> None:
    RETRY_429.update({
        "seconds": float(os.environ.get("RETRY_429_SECONDS", "60")),
        "interval": float(os.environ.get("RETRY_429_INTERVAL", "10")),
    })


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser so no python-dotenv dependency is needed."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


@dataclass(frozen=True)
class Account:
    name: str
    key: str


def load_accounts(vibe_env: dict[str, str]) -> list[Account]:
    """The Vibe plan key first, the optional Studio API key as fallback."""
    vibe_key = os.environ.get("VIBE_KEY") or vibe_env.get("MISTRAL_API_KEY") or ""
    studio_key = os.environ.get("STUDIO_API_KEY") or ""
    accounts = []
    if vibe_key:
        accounts.append(Account("vibe", vibe_key))
    if studio_key:
        accounts.append(Account("studio", studio_key))
    if not accounts:
        raise SystemExit("no key found: run `vibe --setup` or set VIBE_KEY / STUDIO_API_KEY")
    return accounts


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class KeyPool:
    def __init__(self, accounts: list[Account]) -> None:
        self.accounts = accounts
        self.state = load_state()  # {account name: {"down_until": epoch, "key": fingerprint}}
        self.lock = threading.Lock()

    def _down_until_locked(self, account: Account) -> float:
        entry = self.state.get(account.name, {})
        if entry.get("key") != key_fingerprint(account.key):
            return 0.0  # cooldown belonged to a key that has since been replaced
        return float(entry.get("down_until") or 0)

    def down_until(self, account: Account) -> float:
        with self.lock:
            return self._down_until_locked(account)

    def mark_down(self, account: Account, why: str, cooldown: float) -> None:
        with self.lock:
            until = time.time() + cooldown
            if self._down_until_locked(account) >= until:
                return
            self.state[account.name] = {"down_until": until, "key": key_fingerprint(account.key)}
            save_state(self.state)
        LOG.warning("account %s cooling down (%s) for %.0fs", account.name, why, cooldown)

    def accounts_for(self) -> list[Account]:
        """Serving keys in order. The Vibe key gets all traffic so its upstream
        prompt cache stays warm; the fallback only steps in while it cools down."""
        now = time.time()
        with self.lock:
            serving = [a for a in self.accounts if now >= self._down_until_locked(a)]
        if not serving:
            return [self.accounts[0]]  # all cooling: try the first anyway
        return serving


POOL: KeyPool | None = None  # set in main()
USAGE_DB_LOCK = threading.Lock()


def usage_db() -> sqlite3.Connection:
    """One row per (day, model, account) in usage.db; updated in place."""
    conn = sqlite3.connect(BASE_DIR / "usage.db", timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage ("
        "day TEXT, model TEXT, account TEXT,"
        "requests INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,"
        "cached_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,"
        "PRIMARY KEY (day, model, account))"
    )
    return conn


def record_usage(model: str, account: str, usage: dict) -> None:
    """Upsert one request's usage into today's row; errors never kill a reply."""
    day = time.strftime("%Y-%m-%d")
    prompt = int(usage.get("prompt_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    completion = int(usage.get("completion_tokens") or 0)
    with USAGE_DB_LOCK:
        try:
            with usage_db() as conn:
                conn.execute(
                    "UPDATE usage SET requests = requests + 1,"
                    " prompt_tokens = prompt_tokens + ?,"
                    " cached_tokens = cached_tokens + ?,"
                    " completion_tokens = completion_tokens + ?"
                    " WHERE day = ? AND model = ? AND account = ?",
                    (prompt, cached, completion, day, model, account),
                )
                if conn.total_changes == 0:
                    conn.execute(
                        "INSERT INTO usage (day, model, account, requests,"
                        " prompt_tokens, cached_tokens, completion_tokens)"
                        " VALUES (?, ?, ?, 1, ?, ?, ?)",
                        (day, model, account, prompt, cached, completion),
                    )
        except sqlite3.Error as e:
            LOG.warning("usage write failed: %s", e)


def upstream_request(url: str, key: str, body: bytes | None, method: str = "POST"):
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=600)


def upstream_request_with_retry(
    url: str, key: str, name: str, body: bytes | None, method: str = "POST"
):
    """Like upstream_request, but a 429 retries the same key within the window."""
    deadline = time.time() + RETRY_429["seconds"]
    while True:
        try:
            return upstream_request(url, key, body, method)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            remaining = deadline - time.time()
            if remaining <= 0:
                raise  # window spent
            try:
                detail = e.read()
            except OSError:
                detail = b""
            e.close()
            LOG.info("key=%s rejected: %s %s", name, e.code, detail[:200])
            time.sleep(min(RETRY_429["interval"], remaining))


def read_ratelimit_headers(resp) -> dict:
    return {k.lower(): v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}


def normalize_content_chunks(node):
    """Flatten Mistral GLM content chunks (thinking/text lists) to OpenAI shape.

    GLM replies put message.content as a list of typed parts; many clients
    do string ops on it and crash. Returns (plain_text, reasoning_text).
    """
    if not isinstance(node, list):
        return node, None
    text_parts, think_parts = [], []
    for part in node:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "thinking":
            for t in part.get("thinking", []):
                if isinstance(t, dict):
                    think_parts.append(t.get("text", ""))
        elif part.get("type") == "text":
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            else:
                for t in part.get("text", []):
                    if isinstance(t, dict):
                        text_parts.append(t.get("text", ""))
    return "".join(text_parts), ("".join(think_parts) or None)


def normalize_stream_line(line: bytes, saw_tool_call: bool = False) -> tuple[bytes, bool]:
    """Rewrite one SSE event: GLM chunk-lists become plain fields, and a
    terminal "stop" after tool-call deltas becomes "tool_calls".

    Returns (event_bytes, saw_tool_call); the flag persists across events so
    the finish_reason translation can apply at the stream's last chunk.
    """
    if not line.startswith(b"data: "):
        return line, saw_tool_call
    # The caller passes whole events including the blank separator line;
    # split it off before parsing so the rewritten event keeps its framing.
    body, sep, tail = line.partition(b"\n\n")
    if not sep:
        body = body.rstrip(b"\n")
        tail = b"\n" if line.endswith(b"\n") else b""
    payload = body[6:]
    try:
        parsed = json.loads(payload)
    except ValueError:
        return line, saw_tool_call
    for choice in parsed.get("choices", []):
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, list):
            plain, reasoning = normalize_content_chunks(content)
            delta["content"] = plain
            if reasoning:
                delta["reasoning_content"] = reasoning
        if delta.get("tool_calls"):
            saw_tool_call = True
        if saw_tool_call and choice.get("finish_reason") == "stop":
            choice["finish_reason"] = "tool_calls"
    return b"data: " + json.dumps(parsed).encode() + sep + tail, saw_tool_call


def parse_event(event: bytes) -> dict | None:
    try:
        parsed = json.loads(event[6:].strip())
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_completion(payload: bytes) -> bytes:
    """Rewrite one non-streaming completion to plain OpenAI message shape."""
    try:
        parsed = json.loads(payload)
    except ValueError:
        return payload
    for choice in parsed.get("choices", []):
        msg = choice.get("message")
        if not isinstance(msg, dict):
            continue
        if isinstance(msg.get("content"), list):
            plain, reasoning = normalize_content_chunks(msg["content"])
            msg["content"] = plain
            if reasoning:
                msg["reasoning_content"] = reasoning
        if msg.get("tool_calls") and choice.get("finish_reason") == "stop":
            choice["finish_reason"] = "tool_calls"
    return json.dumps(parsed).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route stdlib noise through logging
        LOG.debug(fmt, *args)

    # --- helpers -------------------------------------------------------
    def _reply(self, code: int, payload, headers: dict | None = None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code >= 400:
            self.send_header("Connection", "close")  # request body may be unread
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = os.environ.get("GATEWAY_KEY", "")
        if not expected:
            return True  # keyless on loopback
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth.removeprefix("Bearer "), expected)

    def _host_allowed(self) -> bool:
        """Keyless mode only answers loopback hostnames, which blocks DNS rebinding."""
        if os.environ.get("GATEWAY_KEY"):
            return True
        host = self.headers.get("Host", "")
        if not host.endswith("]") and ":" in host:
            host = host.rpartition(":")[0]  # strip the port
        if host in LOOPBACK_HOSTS:
            return True
        LOG.warning("rejected Host %r: set GATEWAY_KEY to allow other hostnames", host)
        return False

    def _proxy_body(self, body: bytes, stream: bool) -> None:
        """Try accounts in pool order; stream the winning response downstream."""
        last_err = None
        for account in POOL.accounts_for():
            try:
                upstream = upstream_request_with_retry(
                    MISTRAL_CHAT_URL, account.key, account.name, body
                )
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read()
                except OSError:
                    detail = b""
                e.close()
                last_err = (e.code, detail)
                LOG.info("key=%s rejected: %s %s", account.name, e.code, detail[:200])
                if e.code in (400, 422):
                    break
                if e.code in COOLDOWNS:
                    POOL.mark_down(account, f"HTTP {e.code}", COOLDOWNS[e.code])
                continue
            except OSError as e:
                LOG.warning("key=%s network error: %s", account.name, e)
                last_err = (502, str(e).encode())
                continue

            if not stream:
                # Read the full payload first: headers stay unsent, so a mid-read
                # upstream failure can still fall through to the next account.
                try:
                    payload = normalize_completion(upstream.read())
                except (OSError, http.client.HTTPException) as e:
                    LOG.warning("key=%s died mid-read: %s", account.name, e)
                    last_err = (502, str(e).encode())
                    continue
                finally:
                    upstream.close()
                completion = parse_event(b"data: " + payload) or {}
                record_usage(completion.get("model") or self._req_model, account.name,
                             completion.get("usage") or {})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Pool-Key", account.name)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                LOG.info("served by key=%s", account.name)
                return

            sent = 0
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                rl = read_ratelimit_headers(upstream)
                for k, v in rl.items():
                    self.send_header(f"X-Pool-{k}", f"{account.name}: {v}")
                self.send_header("X-Pool-Key", account.name)
                self.end_headers()
                buf = b""
                saw_tool_call = False
                stream_usage = None
                stream_model = None
                while True:
                    chunk = upstream.read1(8192)  # read() would hold events until 8 KB arrive
                    if not chunk:
                        break
                    buf += chunk
                    # SSE events end with a blank line; only complete lines
                    # are rewritten so a split JSON payload is never mangled.
                    while b"\n\n" in buf:
                        raw_event, buf = buf.split(b"\n\n", 1)
                        event, saw_tool_call = normalize_stream_line(raw_event + b"\n\n", saw_tool_call)
                        self.wfile.write(event)
                        sent += len(event)
                        self.wfile.flush()
                        event_json = parse_event(event) or {}
                        stream_model = event_json.get("model") or stream_model
                        stream_usage = event_json.get("usage") or stream_usage
                if buf:
                    tail_event, saw_tool_call = normalize_stream_line(buf, saw_tool_call)
                    self.wfile.write(tail_event)
                    sent += len(tail_event)
                    self.wfile.flush()
                    event_json = parse_event(tail_event) or {}
                    stream_model = event_json.get("model") or stream_model
                    stream_usage = event_json.get("usage") or stream_usage
                LOG.info("stream complete: key=%s forwarded=%dB tool_calls=%s", account.name, sent, saw_tool_call)
                if stream_usage:
                    record_usage(stream_model or self._req_model, account.name, stream_usage)
            except (OSError, http.client.HTTPException) as e:
                LOG.warning("stream cut: key=%s forwarded=%dB error=%s", account.name, sent, e)
            else:
                LOG.info("served by key=%s", account.name)
            finally:
                upstream.close()
            return

        code, detail = last_err if last_err else (500, b"no keys available")
        self._reply(code, {"error": {"message": detail.decode(errors="replace")[:500], "type": "upstream_error", "code": code}})

    # --- routes --------------------------------------------------------
    def do_GET(self):
        if not self._host_allowed():
            return self._reply(403, {"error": {"message": "forbidden host"}})
        route, _, query = self.path.partition("?")
        if route == "/health":
            if os.environ.get("GATEWAY_KEY") and not self._authorized():
                return self._reply(200, {"ok": True})
            return self._reply(200, {
                "ok": True,
                "accounts": len(POOL.accounts),
                "serving": len(POOL.accounts_for()),
            })
        if not self._authorized():
            return self._reply(401, {"error": {"message": "unauthorized"}})
        if route in ("/v1/models", "/models"):
            for account in POOL.accounts_for():
                try:
                    upstream = upstream_request_with_retry(
                        MISTRAL_MODELS_URL, account.key, account.name, None, method="GET"
                    )
                except urllib.error.HTTPError as e:
                    e.close()
                    LOG.info("models via key=%s failed: %s", account.name, e)
                    if e.code in COOLDOWNS:
                        POOL.mark_down(account, f"HTTP {e.code}", COOLDOWNS[e.code])
                    continue
                except OSError as e:
                    LOG.info("models via key=%s failed: %s", account.name, e)
                    continue
                try:
                    models = json.loads(upstream.read())
                except (OSError, http.client.HTTPException, ValueError) as e:
                    LOG.info("models read via key=%s failed: %s", account.name, e)
                    continue
                finally:
                    upstream.close()
                return self._reply(200, models)
            return self._reply(502, {"error": {"message": "all keys failed"}})
        if route == "/pool/status":
            now = time.time()
            accounts = []
            for account in POOL.accounts:
                down_until = POOL.down_until(account)
                accounts.append({
                    "name": account.name,
                    "cooling": now < down_until,
                    "down_remaining_s": max(0, int(down_until - now)),
                })
            return self._reply(200, {
                "accounts": accounts,
                "serving": [account.name for account in POOL.accounts_for()],
            })
        if route == "/usage":
            # Optional ?days=N limits the window; default is everything logged.
            params = urllib.parse.parse_qs(query)
            try:
                window_days = int(params["days"][0]) if "days" in params else 3650
                since = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
            except (ValueError, OverflowError):
                since = "0000-01-01"
            try:
                with USAGE_DB_LOCK:
                    with usage_db() as conn:
                        rows = conn.execute(
                            "SELECT day, model, account, requests, prompt_tokens,"
                            " cached_tokens, completion_tokens FROM usage"
                            " WHERE day >= ? ORDER BY day DESC, model, account",
                            (since,),
                        ).fetchall()
            except sqlite3.Error as e:
                return self._reply(500, {"error": {"message": f"usage db: {e}"}})
            return self._reply(200, {
                "usage": [
                    {"day": r[0], "model": r[1], "account": r[2], "requests": r[3],
                     "prompt_tokens": r[4], "cached_tokens": r[5], "completion_tokens": r[6]}
                    for r in rows
                ],
            })
        return self._reply(404, {"error": {"message": f"unknown route {self.path}"}})

    def do_POST(self):
        if not self._host_allowed():
            return self._reply(403, {"error": {"message": "forbidden host"}})
        if not self._authorized():
            return self._reply(401, {"error": {"message": "unauthorized"}})
        if self.path not in ("/v1/chat/completions", "/v1/chat/completions/", "/chat/completions"):
            return self._reply(404, {"error": {"message": f"unknown route {self.path}"}})
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._reply(411, {"error": {"message": "Content-Length required"}})
        # application/json forces a CORS preflight, which this server never answers
        if self.headers.get_content_type() != "application/json":
            return self._reply(415, {"error": {"message": "Content-Type must be application/json"}})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._reply(400, {"error": {"message": "invalid Content-Length"}})
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except ValueError:
            req = None
        if not isinstance(req, dict):
            return self._reply(400, {"error": {"message": "invalid JSON body"}})
        stream = bool(req.get("stream"))
        changed = False
        if stream and "stream_options" not in req:
            # Without include_usage the final usage chunk never arrives, and
            # clients that bill tokens show zero for every response.
            req["stream_options"] = {"include_usage": True}
            changed = True
        if "max_tokens" not in req:
            # Mistral caps GLM completions at 4096 tokens when max_tokens is
            # absent; thinking alone eats that, so long-composition turns end
            # mid-reasoning with no visible text.
            req["max_tokens"] = 32768
            changed = True
        msgs = req.get("messages")
        if (
            "reasoning_effort" not in req
            and req.get("tools")
            and isinstance(msgs, list)
            and msgs
            and isinstance(msgs[-1], dict)
            and msgs[-1].get("role") == "tool"
        ):
            # GLM sometimes terminates tool-result composition turns with zero
            # visible text ("stop", completion_tokens 0) after a long reasoning
            # phase. Raising the effort to high counterintuitively avoids the
            # premature stop: the model finishes deliberating and composes.
            req["reasoning_effort"] = "high"
            changed = True
        if changed:
            body = json.dumps(req).encode()
        self._req_model = req.get("model") or "unknown"
        self._proxy_body(body, stream)


def main() -> int:
    os.environ.update(read_env_file(BASE_DIR / "gateway.env"))
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    vibe_env = read_env_file(pathlib.Path(
        os.environ.get("VIBE_ENV_FILE", str(pathlib.Path.home() / ".vibe/.env"))))
    build_cooldowns()
    build_retries()

    global POOL
    POOL = KeyPool(load_accounts(vibe_env))
    for account in POOL.accounts:
        if account.name != "vibe":
            continue
        try:
            with upstream_request(MISTRAL_WHOAMI_URL, account.key, None, method="GET") as resp:
                whoami = json.loads(resp.read())
            LOG.info("vibe plan: %s (%s)", whoami.get("plan_name"), whoami.get("plan_type"))
        except (urllib.error.HTTPError, OSError, ValueError) as e:
            LOG.warning("vibe whoami failed (continuing anyway): %s", e)

    port = int(os.environ.get("PORT", "8788"))
    addr = os.environ.get("BIND", "127.0.0.1")
    httpd = ThreadingHTTPServer((addr, port), Handler)
    LOG.info("mistral gateway on %s:%d (keys: %s)", addr, port,
             ", ".join(account.name for account in POOL.accounts))

    def shutdown(signum, frame):
        LOG.info("signal %s, shutting down", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    httpd.serve_forever(poll_interval=POLL_SLEEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
