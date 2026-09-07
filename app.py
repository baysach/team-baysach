from __future__ import annotations

import hmac
import json
import queue
import secrets
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
import websocket
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


APP_HOST = "127.0.0.1"
APP_PORT = 8765
POE_HOST = "www.pathofexile.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
FETCH_PSEUDOS = (
    "pseudo.pseudo_number_of_empty_affix_mods",
    "pseudo.pseudo_number_of_prefix_mods",
    "pseudo.pseudo_number_of_suffix_mods",
)

app = Flask(__name__)
app.config.update(JSON_SORT_KEYS=False, MAX_CONTENT_LENGTH=1_000_000)
CSRF_TOKEN = secrets.token_urlsafe(32)


@dataclass(frozen=True)
class SearchTarget:
    url: str
    realm: str
    league: str


def parse_search_url(value: str) -> SearchTarget:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != POE_HOST:
        raise ValueError(f"Search URL must use https://{POE_HOST}.")
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("Search URL must not contain credentials, a port, query, or fragment.")

    parts = parsed.path.strip("/").split("/")
    if len(parts) != 5 or parts[:3] != ["api", "trade2", "search"]:
        raise ValueError("Search URL must match /api/trade2/search/<realm>/<league>.")

    realm = unquote(parts[3]).strip()
    league = unquote(parts[4]).strip()
    if not realm or not league or "/" in realm or "/" in league:
        raise ValueError("Search URL contains an invalid realm or league.")
    return SearchTarget(url=value, realm=realm, league=league)


def validate_cookie(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Cookie Header is required.")
    if "\r" in value or "\n" in value:
        raise ValueError("Cookie Header must be a single line.")
    if "POESESSID=" not in value:
        raise ValueError("Cookie Header does not contain POESESSID.")
    return value


def validate_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Search Payload must be a JSON object.")
    if not isinstance(value.get("query"), dict):
        raise ValueError("Search Payload must contain a query object.")
    return value


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=150)
        self._listeners: set[queue.Queue[dict[str, Any]]] = set()

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._history.append(event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> tuple[queue.Queue[dict[str, Any]], list[dict[str, Any]]]:
        listener: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            self._listeners.add(listener)
            history = list(self._history)
        return listener, history

    def unsubscribe(self, listener: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._listeners.discard(listener)


class PoeMonitor:
    def __init__(self, events: EventBus) -> None:
        self.events = events
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._alerts: queue.Queue[str | None] = queue.Queue()
        self._ws: websocket.WebSocketApp | None = None
        self._running = False
        self._authenticated = False
        self._processed_ids: set[str] = set()
        self._processed_alerts: set[str] = set()
        self._session: requests.Session | None = None
        self._target: SearchTarget | None = None
        self._payload: dict[str, Any] | None = None
        self._cookie: str | None = None
        self._send_whispers = False
        self._busy = False
        self._query_id: str | None = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "authenticated": self._authenticated,
                "mode": "send" if self._send_whispers else "dry-run",
                "busy": self._busy,
                "query_id": self._query_id,
            }

    def start(
        self,
        *,
        cookie: str,
        target: SearchTarget,
        payload: dict[str, Any],
        send_whispers: bool,
    ) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("The monitor is already running.")
            self._running = True
            self._authenticated = False
            self._query_id = None
            self._send_whispers = send_whispers
            self._busy = False
            self._target = target
            self._payload = payload
            self._cookie = cookie
            self._processed_ids.clear()
            self._processed_alerts.clear()
            self._stop.clear()
            self._alerts = queue.Queue()
            self._thread = threading.Thread(target=self._run, name="poe-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._alerts.put(None)
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._emit("info", "Stopping monitor…")

    def ready_for_next(self) -> None:
        with self._lock:
            if not self._running:
                raise RuntimeError("The monitor is not running.")
            if not self._send_whispers:
                raise RuntimeError("Ready for Next Alert is only used in travel mode.")
            if not self._busy:
                raise RuntimeError("The monitor is already ready for the next alert.")
            self._busy = False
        self._emit_state()
        self._emit("success", "Ready for the next alert.")

    def _emit(self, level: str, message: str, **details: Any) -> None:
        event = {
            "type": "log",
            "level": level,
            "message": message,
            "time": time.strftime("%H:%M:%S"),
        }
        if details:
            event["details"] = details
        self.events.publish(event)

    def _emit_state(self) -> None:
        self.events.publish({"type": "state", **self.state()})

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "*/*",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
                "Content-Type": "application/json",
                "Cookie": self._cookie or "",
                "Origin": f"https://{POE_HOST}",
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(429, 500, 502, 503, 504),
            backoff_factor=1,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        session.mount("https://", HTTPAdapter(max_retries=retries))
        return session

    def _run(self) -> None:
        try:
            self._session = self._make_session()
            self._create_search()
            if self._stop.is_set():
                return
            self._worker = threading.Thread(target=self._process_alerts, name="poe-alerts", daemon=True)
            self._worker.start()
            self._listen_forever()
        except Exception as exc:
            self._emit("error", self._safe_error(exc))
        finally:
            self._stop.set()
            self._alerts.put(None)
            if self._session is not None:
                self._session.close()
            with self._lock:
                self._running = False
                self._authenticated = False
                self._busy = False
                self._query_id = None
                self._cookie = None
                self._payload = None
                self._session = None
                self._ws = None
            self._emit_state()
            self._emit("info", "Monitor stopped. Credentials were cleared from memory.")

    def _create_search(self) -> None:
        assert self._session is not None and self._target is not None and self._payload is not None
        self._emit("info", "Creating the custom search…")
        response = self._session.post(
            self._target.url,
            json=self._payload,
            headers={"Referer": self._target.url.replace("/api/", "/")},
            timeout=30,
        )
        self._raise_for_status(response, "Search request")
        data = response.json()
        query_id = data.get("id")
        if not isinstance(query_id, str) or not query_id:
            raise RuntimeError("Search response did not contain a query ID.")
        with self._lock:
            self._query_id = query_id
        self._emit("success", "Search created.", query_id=query_id)
        self._emit_state()

    def _listen_forever(self) -> None:
        assert self._target is not None and self._query_id is not None
        league = quote(self._target.league, safe="")
        realm = quote(self._target.realm, safe="")
        query = quote(self._query_id, safe="")
        ws_url = f"wss://{POE_HOST}/api/trade2/live/{realm}/{league}/{query}"
        delay = 2

        while not self._stop.is_set():
            self._emit("info", "Connecting to live search…")
            self._ws = websocket.WebSocketApp(
                ws_url,
                cookie=self._cookie,
                header=[f"User-Agent: {USER_AGENT}", "Cache-Control: no-cache", "Pragma: no-cache"],
                on_message=self._on_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
            )
            try:
                self._ws.run_forever(
                    origin=f"https://{POE_HOST}",
                )
            except Exception as exc:
                self._emit("warning", f"Live connection ended: {self._safe_error(exc)}")
            finally:
                self._ws = None

            if self._stop.is_set():
                break
            with self._lock:
                was_authenticated = self._authenticated
                self._authenticated = False
            if was_authenticated:
                delay = 2
            self._emit_state()
            self._emit("warning", f"Live connection lost. Reconnecting in {delay} seconds…")
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def _on_message(self, _ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._emit("warning", "Received an unreadable live-search message.")
            return

        if "auth" in data:
            authenticated = data.get("auth") is True
            with self._lock:
                self._authenticated = authenticated
            self._emit_state()
            if authenticated:
                self._emit("success", "Live search authenticated. Listening for new listings…")
            else:
                self._emit("error", "Live-search authentication failed. Refresh your Cookie Header.")
                self._stop.set()
                _ws.close()
            return

        alert_token = data.get("result")
        if isinstance(alert_token, str) and alert_token:
            if alert_token in self._processed_alerts:
                return
            self._processed_alerts.add(alert_token)
            count = data.get("count")
            self._emit("info", "New live alert received.", count=count, sound=True)
            self._alerts.put(alert_token)

    def _on_ws_error(self, _ws: websocket.WebSocketApp, error: Any) -> None:
        if self._stop.is_set():
            return
        close_details = self._close_frame_details(error)
        if close_details is not None:
            status_code, reason = close_details
            suffix = f": {reason}" if reason else ""
            self._emit(
                "warning",
                f"Live connection closed by server with status {status_code}{suffix}.",
            )
            return
        self._emit("warning", f"Live connection warning: {self._safe_error(error)}")

    def _on_ws_close(
        self,
        _ws: websocket.WebSocketApp,
        status_code: int | None,
        _message: str | None,
    ) -> None:
        if not self._stop.is_set() and status_code:
            suffix = f": {_message}" if _message else ""
            self._emit("warning", f"Live connection closed with status {status_code}{suffix}.")

    @staticmethod
    def _close_frame_details(error: Any) -> tuple[int, str] | None:
        if not isinstance(error, websocket.ABNF) or error.opcode != websocket.ABNF.OPCODE_CLOSE:
            return None
        data = error.data
        if not isinstance(data, bytes) or len(data) < 2:
            return None
        status_code = int.from_bytes(data[:2], "big")
        reason = data[2:].decode("utf-8", errors="replace").strip()
        return status_code, reason

    def _process_alerts(self) -> None:
        while not self._stop.is_set():
            try:
                alert_token = self._alerts.get(timeout=1)
            except queue.Empty:
                continue
            if alert_token is None:
                break
            try:
                self._fetch_alert(alert_token)
            except Exception as exc:
                self._emit("error", f"Could not process alert: {self._safe_error(exc)}")

    def _fetch_alert(self, alert_token: str) -> None:
        assert self._session is not None and self._target is not None and self._query_id is not None
        fetch_url = f"https://{POE_HOST}/api/trade2/fetch/{alert_token}"
        params: list[tuple[str, str]] = [("query", self._query_id), ("realm", self._target.realm)]
        params.extend(("pseudos[]", pseudo) for pseudo in FETCH_PSEUDOS)
        response = self._session.get(
            fetch_url,
            params=params,
            headers={"Referer": self._trade_referer(live=True)},
            timeout=30,
        )
        self._raise_for_status(response, "Listing fetch")
        results = response.json().get("result")
        if not isinstance(results, list):
            raise RuntimeError("Listing fetch returned an unexpected response.")
        if not results:
            self._emit("warning", "The live alert returned no listings.")
            return
        for result in results:
            if isinstance(result, dict):
                self._handle_listing(result)

    def _handle_listing(self, result: dict[str, Any]) -> None:
        listing_id = result.get("id")
        if not isinstance(listing_id, str) or not listing_id:
            self._emit("warning", "Skipped a listing with no ID.")
            return
        if listing_id in self._processed_ids:
            return
        self._processed_ids.add(listing_id)

        listing = result.get("listing") if isinstance(result.get("listing"), dict) else {}
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        account = listing.get("account") if isinstance(listing.get("account"), dict) else {}
        price = listing.get("price") if isinstance(listing.get("price"), dict) else {}
        seller = account.get("name") or "Unknown seller"
        item_name = " ".join(part for part in (item.get("name"), item.get("typeLine")) if part).strip()
        item_name = item_name or "Unknown item"
        if price:
            price_text = f"{price.get('amount', '?')} {price.get('currency', 'unknown')}"
        else:
            price_text = "No listed price"
        short_id = f"{listing_id[:10]}…{listing_id[-8:]}" if len(listing_id) > 22 else listing_id

        self._emit(
            "listing",
            f"{item_name} — {price_text}",
            seller=seller,
            listing_id=short_id,
        )

        hideout_token = listing.get("hideout_token")
        if not isinstance(hideout_token, str) or not hideout_token:
            self._emit("warning", f"No hideout token was available for {item_name}.")
            return
        if not self._send_whispers:
            self._emit("dry-run", f"Dry run: travel not requested for {item_name}.")
            return
        with self._lock:
            busy = self._busy
        if busy:
            self._emit(
                "warning",
                f"Skipped {item_name} while you are busy with the current trade.",
                listing_id=short_id,
            )
            return
        self._send_whisper(hideout_token, item_name, short_id)

    def _send_whisper(self, hideout_token: str, item_name: str, short_id: str) -> None:
        assert self._session is not None
        for attempt in range(2):
            payload = {"token": hideout_token}
            if attempt == 1:
                payload["continue"] = True
            response = self._session.post(
                f"https://{POE_HOST}/api/trade2/whisper",
                json=payload,
                headers={"Referer": self._trade_referer(live=True)},
                timeout=30,
            )
            try:
                data = response.json()
            except ValueError:
                data = None
            needs_confirmation = data is False or (
                isinstance(data, dict)
                and data.get("success") is False
                and not data.get("error")
            )
            if attempt == 0 and response.ok and needs_confirmation:
                if self._stop.is_set():
                    return
                self._emit("info", f"Continuing travel for {item_name} after the site's demand confirmation.")
                continue
            break
        accepted = data is True or (isinstance(data, dict) and data.get("success") is True)
        if not response.ok or not accepted:
            message = (
                data.get("error") or data.get("message")
                if isinstance(data, dict)
                else None
            ) or "The site did not confirm success."
            if isinstance(message, dict):
                message = message.get("message") or str(message)
            self._emit(
                "error",
                f"Travel failed for {item_name}: {message}",
                http_status=response.status_code,
                response_body=response.text,
            )
            self._raise_for_status(response, "Travel request")
            return
        with self._lock:
            self._busy = True
        self._emit("success", f"Travel accepted for {item_name}.", listing_id=short_id)
        self._emit_state()
        self._emit("warning", "Travel paused. Click Ready for Next Alert when your trade is finished.")

    def _trade_referer(self, *, live: bool) -> str:
        assert self._target is not None and self._query_id is not None
        league = quote(self._target.league, safe="")
        suffix = "/live" if live else ""
        return (
            f"https://{POE_HOST}/trade2/search/{quote(self._target.realm, safe='')}/"
            f"{league}/{quote(self._query_id, safe='')}{suffix}"
        )

    @staticmethod
    def _raise_for_status(response: requests.Response, label: str) -> None:
        if response.ok:
            return
        safe_message = ""
        try:
            data = response.json()
            candidate = data.get("error") or data.get("message")
            if isinstance(candidate, dict):
                candidate = candidate.get("message")
            if isinstance(candidate, str):
                safe_message = f": {candidate[:300]}"
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}{safe_message}")

    @staticmethod
    def _safe_error(error: Any) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return message[:500] or error.__class__.__name__


EVENTS = EventBus()
MONITOR = PoeMonitor(EVENTS)


@app.before_request
def protect_local_app() -> tuple[str, int] | None:
    hostname = request.host.partition(":")[0].strip("[]").lower()
    if hostname not in {"127.0.0.1", "localhost"}:
        return "Local access only", 403
    return None


@app.after_request
def secure_response(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self'; "
        "style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'"
    )
    return response


def require_csrf() -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(supplied, CSRF_TOKEN):
        raise PermissionError("Invalid local request token.")


@app.get("/")
def index() -> str:
    return render_template("index.html", csrf_token=CSRF_TOKEN)


@app.get("/api/state")
def api_state() -> Response:
    return jsonify(MONITOR.state())


@app.post("/api/start")
def api_start() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        cookie = validate_cookie(data.get("cookie", ""))
        target = parse_search_url(data.get("search_url", ""))
        payload = validate_payload(data.get("payload"))
        send_whispers = data.get("send_whispers") is True
        MONITOR.start(
            cookie=cookie,
            target=target,
            payload=payload,
            send_whispers=send_whispers,
        )
        return jsonify({"ok": True, "mode": "send" if send_whispers else "dry-run"})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except (ValueError, RuntimeError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/stop")
def api_stop() -> tuple[Response, int] | Response:
    try:
        require_csrf()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    MONITOR.stop()
    return jsonify({"ok": True})


@app.post("/api/ready")
def api_ready() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        MONITOR.ready_for_next()
        return jsonify({"ok": True})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/events")
def api_events() -> Response:
    listener, history = EVENTS.subscribe()

    @stream_with_context
    def generate():
        try:
            for event in history:
                yield f"data: {json.dumps({**event, 'replayed': True}, separators=(',', ':'))}\n\n"
            while True:
                try:
                    event = listener.get(timeout=15)
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            EVENTS.unsubscribe(listener)

    return Response(generate(), mimetype="text/event-stream")


def main() -> None:
    threading.Timer(0.8, lambda: webbrowser.open(f"http://{APP_HOST}:{APP_PORT}")).start()
    print(f"POE Live Alert is running at http://{APP_HOST}:{APP_PORT}")
    print("Press Ctrl+C to stop it.")
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
