"""Ollama's API in front of a local OpenAI-compatible model server.

OpenAmika's fully local route talks to Ollama. Some models do not run there: NVIDIA's NVFP4 checkpoints, for one,
are served by vLLM. This program answers the part of Ollama's API that OpenAmika uses and passes each call on to a
server that speaks the OpenAI API, so the Setup page's "Ollama server" field can point at it and nothing in
OpenAmika changes:

    python contrib/ollama-bridge/ollama_bridge.py --backend http://127.0.0.1:8000/v1

It is for local servers only, by construction. The backend must be plain http on this machine or a private
network, checked again on every call, and no API key is read, stored or sent.

Answered (the caller is brain/f_ollama.py): GET /api/version, /api/tags, /api/ps; POST /api/show, /api/chat.
Standard library only.
"""
import argparse
import http.client
import ipaddress
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

VERSION = "0.1.0"
DEFAULT_LISTEN = "127.0.0.1:11435"      # beside a real Ollama on 11434, not in its place
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 600                    # the patience OpenAmika has with Ollama itself
IDLE_TIMEOUT_S = 60                     # a caller that sends nothing for this long gives its thread back
MAX_BODY = 8 << 20
# Ollama option -> OpenAI field. num_ctx and keep_alive have no counterpart: the server fixed both when it started.
OPTIONS = {"num_predict": "max_tokens", "temperature": "temperature", "top_p": "top_p", "seed": "seed", "stop": "stop"}
THINKING_FIELDS = ("reasoning_content", "reasoning")    # where vLLM's reasoning parsers have put it over time


class BridgeError(Exception):
    """An answer for the caller: an HTTP status and the text Ollama would put in {"error": ...}."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def local_addresses(host, port):
    """The addresses a host name stands for, none of them on the public internet. Raises BridgeError otherwise."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise BridgeError(502, f"{host} does not resolve: {error}") from None
    addresses = [info[4][0] for info in found]
    for address in addresses:
        if ipaddress.ip_address(address.split("%")[0]).is_global:
            raise BridgeError(502, f"{host} is a public address ({address}); the bridge only talks to local servers")
    return addresses


def error_text(raw):
    """The reason inside a server's error body, whichever of the usual shapes it has."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw.strip()[:240]
    if isinstance(data, dict):
        data = data.get("error", data)
    if isinstance(data, dict):
        data = data.get("message") or data.get("detail") or ""
    return str(data or "").strip()[:240]


class Backend:
    """The OpenAI-compatible server behind the bridge: plain http, local, no key."""

    def __init__(self, url):
        parts = urlsplit(str(url or "").strip())
        try:
            port = parts.port or 80
        except ValueError:
            port = None
        if (parts.scheme != "http" or not parts.hostname or port is None or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("The backend must look like http://127.0.0.1:8000/v1: plain http, no credentials")
        self.host, self.port = parts.hostname, port
        self.prefix = parts.path.rstrip("/") or "/v1"
        self.url = f"http://{parts.netloc}{self.prefix}"
        self.answered_at = None                         # the address that took the last connection
        try:
            local_addresses(self.host, self.port)       # refuse a public address at start, not at her first turn
        except BridgeError as error:
            raise ValueError(error.message) from None

    def connect(self):
        """A connection to the model server, at the first of the name's addresses that takes one. A name can stand
        for several: on Windows "localhost" is ::1 before 127.0.0.1, and a server started on 127.0.0.1 answers only
        the second. The address that answered last is tried first, then IPv4, so only a first call pays for a refusal."""
        addresses = list(dict.fromkeys(local_addresses(self.host, self.port)))     # every call: a name can change
        addresses.sort(key=lambda address: (address != self.answered_at, ":" in address))
        refusal = None
        for address in addresses:
            connection = http.client.HTTPConnection(address, self.port, timeout=CONNECT_TIMEOUT_S)
            try:
                connection.connect()
            except OSError as error:
                connection.close()
                refusal = error
                continue
            self.answered_at = address
            return connection
        raise BridgeError(502, f"The model server at {self.url} is not answering: {refusal}")

    def request(self, method, path, body=None):
        """Send one request. Returns (connection, response); the caller closes the connection."""
        connection = self.connect()
        headers = {"Host": f"{self.host}:{self.port}", "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, self.prefix + path, body=payload, headers=headers)
            connection.sock.settimeout(READ_TIMEOUT_S)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise BridgeError(502, f"The model server at {self.url} is not answering: {error}") from None
        if response.status >= 400:
            detail = error_text(response.read(4096))
            connection.close()
            raise BridgeError(response.status, detail or f"The model server answered HTTP {response.status}")
        return connection, response

    def models(self):
        """The rows of GET /models that name a model."""
        connection, response = self.request("GET", "/models")
        try:
            data = json.loads(response.read())
        except (ValueError, OSError, http.client.HTTPException):
            raise BridgeError(502, "The model server's model list is not JSON") from None
        finally:
            connection.close()
        rows = data.get("data") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]]


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def count(value):
    """A token count as the server gave it; 0 when it is not a number (a count must never break an answer)."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def failed(answer):
    """True for an error object: {"error": ...}, or the older {"object": "error", "message": ...}."""
    return isinstance(answer, dict) and bool(answer.get("error") or answer.get("object") == "error")


def tag(row):
    """One served model as a row of /api/tags. Its size on disk and its quantization are not ours to know."""
    return {"name": row["id"], "model": row["id"], "modified_at": now(), "digest": "",
            "details": {"format": "openai-compatible", "family": "", "families": [], "parameter_size": "",
                        "quantization_level": ""}}


def show(row, think_switch):
    """One served model as /api/show describes it: what OpenAmika reads is the capabilities and the context length."""
    info = {"general.architecture": "openai"}
    context = row.get("max_model_len")                  # vLLM reports the window it was started with
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        info["openai.context_length"] = context
    return {"capabilities": ["completion"] + (["thinking"] if think_switch else []), "details": tag(row)["details"],
            "model_info": info, "modified_at": now()}


def chat_request(body, think_switch=True):
    """Ollama's /api/chat body as an OpenAI chat-completions body. Raises BridgeError(400) for what it cannot carry."""
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"]:
        raise BridgeError(400, "model is required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError(400, "messages are required")
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)):
            raise BridgeError(400, "only text messages are supported")
    stream = body.get("stream", True) is not False
    out = {"model": body["model"], "stream": stream,
           "messages": [{"role": message["role"], "content": message["content"]} for message in messages]}
    if stream:
        out["stream_options"] = {"include_usage": True}     # the token counts OpenAmika books come with the last chunk
    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    for name, field in OPTIONS.items():
        if options.get(name) is not None:
            out[field] = options[name]
    if think_switch and isinstance(body.get("think"), bool):
        out["chat_template_kwargs"] = {"enable_thinking": body["think"]}
    wanted = body.get("format")
    if wanted == "json":
        out["response_format"] = {"type": "json_object"}
    elif isinstance(wanted, dict):
        out["response_format"] = {"type": "json_schema", "json_schema": {"name": "response", "schema": wanted}}
    return out


class Reply:
    """What one chat call has produced so far: enough to write Ollama's lines, the last one included."""

    def __init__(self, model):
        self.model, self.started = model, time.monotonic()
        self.finish, self.prompt_tokens, self.completion_tokens = None, 0, 0
        self.answered = False                   # the server has sent a choice: there is a reply, however short

    def take(self, chunk):
        """One OpenAI chunk, or a whole completion: returns (content, thinking), keeps the usage and finish reason."""
        if not isinstance(chunk, dict):
            return "", ""
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.prompt_tokens = count(usage.get("prompt_tokens"))
            self.completion_tokens = count(usage.get("completion_tokens"))
        choices = chunk.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        self.answered = self.answered or bool(choice)
        if choice.get("finish_reason"):
            self.finish = str(choice["finish_reason"])
        part = choice.get("delta") or choice.get("message")
        if not isinstance(part, dict):
            return "", ""
        content = part.get("content") if isinstance(part.get("content"), str) else ""
        thinking = next((part[name] for name in THINKING_FIELDS if isinstance(part.get(name), str)), "")
        return content, thinking

    def line(self, content="", thinking="", done=False):
        message = {"role": "assistant", "content": content}
        if thinking:
            message["thinking"] = thinking
        out = {"model": self.model, "created_at": now(), "message": message, "done": done}
        if done:
            out.update(done_reason=self.finish or "stop", prompt_eval_count=self.prompt_tokens,
                       eval_count=self.completion_tokens, total_duration=int((time.monotonic() - self.started) * 1e9))
        return out


def events(response, reply):
    """The JSON objects of a server-sent event stream. Raises BridgeError when the stream fails or breaks off."""
    ended = False
    try:
        for raw in response:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                ended = True
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if failed(event):                   # also when "[DONE]" follows it: what came before is not a reply
                raise BridgeError(502, error_text(event) or "The model server reported an error")
            yield event
    except (OSError, http.client.HTTPException) as error:
        raise BridgeError(502, f"The model server stopped answering: {error}") from None
    if not ended and reply.finish is None:
        # Without this an answer cut off by a crashed server would be booked as a complete one.
        raise BridgeError(502, "The model server ended the answer before it was finished")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ollama-bridge/" + VERSION
    timeout = IDLE_TIMEOUT_S                    # between requests and while one is read; an answer gets more (chat)

    def log_message(self, format, *args):
        if self.server.verbose:                 # the request line and status only: never a message's text
            sys.stderr.write("ollama-bridge: " + format % args + "\n")

    def do_GET(self):
        self.answer({"/": self.root, "/api/version": self.version, "/api/tags": self.tags, "/api/ps": self.tags})

    def do_HEAD(self):
        self.answer({"/": self.root})

    def do_POST(self):
        self.answer({"/api/show": self.show, "/api/chat": self.chat})

    def answer(self, routes):
        try:
            self.unread = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            self.unread = 0
        try:
            self.refuse_web_pages()
            route = routes.get(self.path.split("?")[0].rstrip("/") or "/")
            if route is None:
                raise BridgeError(404, "The bridge does not answer this part of Ollama's API")
            route()
        except BridgeError as error:
            self.report(error)
            self.send_json(error.status, {"error": error.message}, close=not self.discard_body())
        except OSError:
            self.close_connection = True        # OpenAmika hung up: there is nobody left to answer

    def report(self, error):
        """OpenAmika shows a failing Ollama as "not answering" and no more, so the reason is printed here."""
        if error.status >= 500 or self.server.verbose:
            sys.stderr.write(f"ollama-bridge: {self.path} -> {error.status} {error.message}\n")

    def refuse_web_pages(self):
        """There is no key on this API, so a page in a browser must not be able to use it: no cross-site request,
        and, while listening on this machine only, no name that is not this machine's (DNS rebinding)."""
        if self.headers.get("Origin"):
            raise BridgeError(403, "Requests from web pages are refused")
        if not self.server.loopback_only:
            return
        name = (self.headers.get("Host") or "").strip()
        name = name[1:name.index("]")] if name.startswith("[") and "]" in name else name.rsplit(":", 1)[0]
        try:
            local = name == "localhost" or ipaddress.ip_address(name).is_loopback
        except ValueError:
            local = False
        if not local:
            raise BridgeError(403, "This bridge listens on this machine only")

    def body(self):
        if not 0 < self.unread <= MAX_BODY:
            raise BridgeError(400, "A JSON body is required")
        data, self.unread = self.rfile.read(self.unread), 0
        try:
            return json.loads(data)
        except ValueError:
            raise BridgeError(400, "The request body is not JSON") from None

    def discard_body(self):
        """Read a request body nobody will use. Closing over unread bytes resets the connection on Windows, and the
        caller then never sees why it was refused. False when the body is too large to read: the connection closes."""
        if self.unread > MAX_BODY:
            return False
        if self.unread:
            self.rfile.read(self.unread)
            self.unread = 0
        return True

    def send_json(self, status, value, close=False):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_line(self, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def root(self):
        data = b"Ollama is running"             # what Ollama itself answers here; some callers probe for it
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def version(self):
        # OpenAmika's Setup page and pre-flight ask only this to decide that Ollama is running. With the model server
        # down they must hear "not answering", or they report a running Ollama and then blame a model "not pulled".
        self.server.backend.models()
        self.send_json(200, {"version": VERSION + " (ollama-bridge)"})

    def tags(self):
        self.send_json(200, {"models": [tag(row) for row in self.server.backend.models()]})

    def show(self):
        body = self.body()
        name = body.get("model") or body.get("name") if isinstance(body, dict) else None
        for row in self.server.backend.models():
            if row["id"] == name:
                return self.send_json(200, show(row, self.server.think_switch))
        raise BridgeError(404, f"model '{name}' not found")

    def chat(self):
        request = chat_request(self.body(), self.server.think_switch)
        reply = Reply(request["model"])
        connection, response = self.server.backend.request("POST", "/chat/completions", request)
        try:
            # An answer is on its way: a caller that reads it slowly gets the patience Ollama has, not an idler's.
            self.connection.settimeout(READ_TIMEOUT_S)
            if not request["stream"]:
                try:
                    whole = json.loads(response.read())
                except (ValueError, OSError, http.client.HTTPException):
                    raise BridgeError(502, "The model server's answer is not JSON") from None
                if failed(whole):
                    raise BridgeError(502, error_text(whole) or "The model server reported an error")
                content, thinking = reply.take(whole)
                if not reply.answered:          # no choice in it: an empty reply would be booked as a complete one
                    raise BridgeError(502, "The model server's answer has no reply in it")
                return self.send_json(200, reply.line(content, thinking, done=True))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for event in events(response, reply):
                    content, thinking = reply.take(event)
                    if content or thinking:
                        self.send_line(reply.line(content, thinking))
                if not reply.answered:          # "[DONE]" and nothing before it: as for a whole answer, not a reply
                    raise BridgeError(502, "The model server's answer has no reply in it")
                self.send_line(reply.line(done=True))
            except BridgeError as error:        # the headers are out: say it the way Ollama does mid-answer
                self.report(error)
                self.send_line({"error": error.message})
            self.wfile.write(b"0\r\n\r\n")
        finally:
            connection.close()                  # also what stops the server generating once OpenAmika hangs up
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"       # on Windows that option lets a second program take the same port

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):
            return          # a caller that drops an idle connection is routine, on Windows by reset: not a traceback
        super().handle_error(request, client_address)


def make_server(backend, listen=DEFAULT_LISTEN, think_switch=True, verbose=False):
    """A bridge ready for serve_forever(). `listen` is host:port; port 0 takes a free one."""
    host, _, port = listen.rpartition(":")
    server = Server((host or "127.0.0.1", int(port)), Handler)
    server.backend = backend if isinstance(backend, Backend) else Backend(backend)
    server.think_switch, server.verbose = think_switch, verbose
    try:
        server.loopback_only = ipaddress.ip_address(server.server_address[0]).is_loopback
    except ValueError:
        server.loopback_only = False
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ollama's API in front of a local OpenAI-compatible model server.")
    parser.add_argument("--backend", required=True, help="the server's OpenAI base, for example http://127.0.0.1:8000/v1")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, help=f"host:port to answer on (default {DEFAULT_LISTEN})")
    parser.add_argument("--no-think-switch", action="store_true",
                        help="do not offer OpenAmika the per-role thinking switch (chat_template_kwargs is then never sent)")
    parser.add_argument("--verbose", action="store_true", help="print one line per request (never a message's text)")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.backend, args.listen, not args.no_think_switch, args.verbose)
    except (ValueError, OSError) as error:
        parser.exit(2, f"ollama-bridge: {error}\n")
    host, port = server.server_address[:2]
    print(f"ollama-bridge {VERSION}: http://{host}:{port} -> {server.backend.url}")
    try:
        names = [row["id"] for row in server.backend.models()]
        print("  serving: " + (", ".join(names) or "no models listed"))
    except BridgeError as error:
        print(f"  the model server is not answering yet ({error.message}); the bridge waits for it")
    print(f"  on OpenAmika's Setup page: Brain -> Ollama -> Ollama server: http://{host}:{port} -> Detect models")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
