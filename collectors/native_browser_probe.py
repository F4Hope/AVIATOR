"""Attach to an already-running local Edge session and observe sanitized live traffic."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
from threading import Event
from time import sleep
from typing import Any

from playwright.sync_api import (
    Browser,
    CDPSession,
    Frame,
    Page,
    Request,
    Response,
    WebSocket,
    sync_playwright,
)

from collectors.network_probe import ProbeWriter, payload_metadata, safe_url


logger = logging.getLogger("aie.collectors.native_browser_probe")


@dataclass(frozen=True, slots=True)
class NativeBrowserProbeConfig:
    target_url: str
    output_path: Path
    cdp_url: str = "http://127.0.0.1:9222"

    def __post_init__(self) -> None:
        if not self.target_url.startswith("https://"):
            raise ValueError("target_url must use https://.")
        if not self.cdp_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("cdp_url must be a localhost HTTP endpoint.")
        if not isinstance(self.output_path, Path) or self.output_path.suffix.lower() != ".jsonl":
            raise ValueError("output_path must be a .jsonl Path.")


def _socket_id(request_id: str) -> str:
    """Return a stable non-sensitive identifier for one CDP WebSocket request id."""
    return hashlib.sha256(request_id.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cdp_frame_payload(response: dict[str, Any]) -> str | bytes:
    """Convert a CDP WebSocketFrame payload to text/bytes for value-free metadata."""
    payload = response.get("payloadData", "")
    if not isinstance(payload, str):
        return b""
    opcode = response.get("opcode")
    if opcode == 1:
        return payload
    try:
        return base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return payload.encode("utf-8", errors="replace")


class NativeBrowserProbe:
    """Observe an already-running local browser without persisting credentials or raw values."""

    def __init__(self, config: NativeBrowserProbeConfig) -> None:
        self.config = config
        self.writer = ProbeWriter(config.output_path)
        self.stop_event = Event()
        self._attached_pages: set[int] = set()
        self._cdp_targets: set[int] = set()
        self._cdp_sessions: list[CDPSession] = []
        self._socket_urls: dict[tuple[int, str], str] = {}

    def _record_response(self, response: Response) -> None:
        try:
            request = response.request
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            event: dict[str, object] = {
                "kind": "response",
                "url": safe_url(response.url),
                "status": response.status,
                "resource_type": request.resource_type,
                "content_type": content_type,
            }
            if "json" in content_type:
                try:
                    body = response.body()
                except Exception:
                    body = b""
                if body:
                    try:
                        text = body.decode("utf-8")
                    except UnicodeDecodeError:
                        text = ""
                    if text:
                        event["payload"] = payload_metadata(text)
            self.writer.append(event)
        except Exception:
            logger.debug("Response observation skipped.", exc_info=False)

    def _record_request_failed(self, request: Request) -> None:
        try:
            failure = request.failure or ""
            self.writer.append({
                "kind": "request_failed",
                "url": safe_url(request.url),
                "resource_type": request.resource_type,
                "failure": payload_metadata(failure),
            })
        except Exception:
            logger.debug("Failed-request observation skipped.", exc_info=False)

    def _record_frame_navigated(self, frame: Frame) -> None:
        try:
            self.writer.append({
                "kind": "frame_navigated",
                "url": safe_url(frame.url),
                "is_main_frame": frame == frame.page.main_frame,
            })
        except Exception:
            logger.debug("Frame navigation observation skipped.", exc_info=False)

    def _record_websocket(self, socket: WebSocket) -> None:
        socket_url = safe_url(socket.url)
        self.writer.append({"kind": "websocket_open", "url": socket_url, "transport": "playwright"})

        def received(payload: str | bytes) -> None:
            try:
                self.writer.append({
                    "kind": "websocket_received",
                    "url": socket_url,
                    "transport": "playwright",
                    "payload": payload_metadata(payload),
                })
            except Exception:
                logger.debug("WebSocket frame observation skipped.", exc_info=False)

        def sent(payload: str | bytes) -> None:
            try:
                self.writer.append({
                    "kind": "websocket_sent",
                    "url": socket_url,
                    "transport": "playwright",
                    "payload": payload_metadata(payload),
                })
            except Exception:
                logger.debug("WebSocket frame observation skipped.", exc_info=False)

        socket.on("framereceived", received)
        socket.on("framesent", sent)

    def _cdp_socket_url(self, session_key: int, request_id: str) -> str:
        return self._socket_urls.get(
            (session_key, request_id),
            f"cdp://websocket/{_socket_id(request_id)}",
        )

    def _record_cdp_websocket_created(self, session_key: int, event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        url = event.get("url")
        if not isinstance(request_id, str) or not isinstance(url, str):
            return
        socket_url = safe_url(url)
        self._socket_urls[(session_key, request_id)] = socket_url
        self.writer.append({
            "kind": "cdp_websocket_open",
            "url": socket_url,
            "socket_id": _socket_id(request_id),
            "transport": "cdp",
        })

    def _record_cdp_websocket_frame(
        self,
        session_key: int,
        event: dict[str, Any],
        direction: str,
    ) -> None:
        try:
            request_id = event.get("requestId")
            response = event.get("response")
            if not isinstance(request_id, str) or not isinstance(response, dict):
                return
            opcode = response.get("opcode")
            self.writer.append({
                "kind": f"cdp_websocket_{direction}",
                "url": self._cdp_socket_url(session_key, request_id),
                "socket_id": _socket_id(request_id),
                "transport": "cdp",
                "opcode": opcode if isinstance(opcode, int) else None,
                "payload": payload_metadata(_cdp_frame_payload(response)),
            })
        except Exception:
            logger.debug("CDP WebSocket frame observation skipped.", exc_info=False)

    def _record_cdp_loading_failed(self, event: dict[str, Any]) -> None:
        try:
            request_id = event.get("requestId")
            error_text = event.get("errorText", "")
            if not isinstance(request_id, str):
                return
            self.writer.append({
                "kind": "cdp_loading_failed",
                "url": f"cdp://request/{_socket_id(request_id)}",
                "resource_type": event.get("type") if isinstance(event.get("type"), str) else "",
                "failure": payload_metadata(error_text if isinstance(error_text, str) else ""),
            })
        except Exception:
            logger.debug("CDP loading failure observation skipped.", exc_info=False)

    def _record_cdp_eventsource(self, event: dict[str, Any]) -> None:
        try:
            request_id = event.get("requestId")
            data = event.get("data", "")
            if not isinstance(request_id, str):
                return
            self.writer.append({
                "kind": "cdp_eventsource_received",
                "url": f"cdp://eventsource/{_socket_id(request_id)}",
                "stream_id": _socket_id(request_id),
                "payload": payload_metadata(data if isinstance(data, str) else ""),
            })
        except Exception:
            logger.debug("CDP EventSource observation skipped.", exc_info=False)

    def _attach_cdp_target(self, target: Page | Frame) -> None:
        identity = id(target)
        if identity in self._cdp_targets:
            return
        try:
            session = target.page.context.new_cdp_session(target) if isinstance(target, Frame) else target.context.new_cdp_session(target)
        except Exception:
            logger.debug("CDP target attachment skipped.", exc_info=False)
            return

        self._cdp_targets.add(identity)
        self._cdp_sessions.append(session)
        session_key = id(session)
        session.on(
            "Network.webSocketCreated",
            lambda event, key=session_key: self._record_cdp_websocket_created(key, event),
        )
        session.on(
            "Network.webSocketFrameReceived",
            lambda event, key=session_key: self._record_cdp_websocket_frame(key, event, "received"),
        )
        session.on(
            "Network.webSocketFrameSent",
            lambda event, key=session_key: self._record_cdp_websocket_frame(key, event, "sent"),
        )
        session.on("Network.loadingFailed", self._record_cdp_loading_failed)
        session.on("Network.eventSourceMessageReceived", self._record_cdp_eventsource)
        session.send("Network.enable")

        target_url = target.url
        self.writer.append({
            "kind": "cdp_network_enabled",
            "url": safe_url(target_url) if target_url else "about:blank",
            "target": "frame" if isinstance(target, Frame) else "page",
        })

    def _attach_page(self, page: Page) -> None:
        identity = id(page)
        if identity in self._attached_pages:
            return
        self._attached_pages.add(identity)
        page.on("response", self._record_response)
        page.on("requestfailed", self._record_request_failed)
        page.on("framenavigated", self._record_frame_navigated)
        page.on("websocket", self._record_websocket)
        page.on("frameattached", self._attach_cdp_target)

        self._attach_cdp_target(page)
        for frame in page.frames:
            if frame != page.main_frame:
                self._attach_cdp_target(frame)

        try:
            self.writer.append({
                "kind": "page_attached",
                "url": safe_url(page.url),
                "title_length": len(page.title()) if page.url else 0,
            })
        except Exception:
            logger.debug("Page metadata observation skipped.", exc_info=False)

    def _attach_browser(self, browser: Browser) -> None:
        if not browser.contexts:
            raise RuntimeError("Local Edge returned no browser context.")
        for context in browser.contexts:
            context.on("page", self._attach_page)
            for page in context.pages:
                self._attach_page(page)

    def run(self) -> None:
        """Attach to the existing local browser and observe until stopped."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.config.cdp_url, timeout=30_000)
            self._attach_browser(browser)
            logger.info("Native probe attached to local Edge over CDP with Network enabled.")
            try:
                while not self.stop_event.is_set():
                    contexts = browser.contexts
                    open_pages = [
                        page
                        for context in contexts
                        for page in context.pages
                        if not page.is_closed()
                    ]
                    if not open_pages:
                        raise RuntimeError("No open browser pages remain.")
                    # A Playwright wait keeps protocol events pumping while remaining read-only.
                    open_pages[0].wait_for_timeout(250)
            finally:
                for session in tuple(self._cdp_sessions):
                    try:
                        session.detach()
                    except Exception:
                        logger.debug("CDP session detach skipped.", exc_info=False)

    def stop(self) -> None:
        self.stop_event.set()
