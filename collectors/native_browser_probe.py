"""Attach to a normal local Edge/Chrome CDP session and observe sanitized traffic."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Event
from time import sleep

from playwright.sync_api import Browser, Page, Response, WebSocket, sync_playwright

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


class NativeBrowserProbe:
    """Observe a normal local browser without persisting credentials or raw values."""

    def __init__(self, config: NativeBrowserProbeConfig) -> None:
        self.config = config
        self.writer = ProbeWriter(config.output_path)
        self.stop_event = Event()
        self._attached_pages: set[int] = set()

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

    def _record_websocket(self, socket: WebSocket) -> None:
        socket_url = safe_url(socket.url)
        self.writer.append({"kind": "websocket_open", "url": socket_url})

        def received(payload: str | bytes) -> None:
            try:
                self.writer.append({
                    "kind": "websocket_received",
                    "url": socket_url,
                    "payload": payload_metadata(payload),
                })
            except Exception:
                logger.debug("WebSocket frame observation skipped.", exc_info=False)

        def sent(payload: str | bytes) -> None:
            try:
                self.writer.append({
                    "kind": "websocket_sent",
                    "url": socket_url,
                    "payload": payload_metadata(payload),
                })
            except Exception:
                logger.debug("WebSocket frame observation skipped.", exc_info=False)

        socket.on("framereceived", received)
        socket.on("framesent", sent)

    def _attach_page(self, page: Page) -> None:
        identity = id(page)
        if identity in self._attached_pages:
            return
        self._attached_pages.add(identity)
        page.on("response", self._record_response)
        page.on("websocket", self._record_websocket)
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
            logger.info("Native probe attached to local Edge over CDP.")
            while not self.stop_event.is_set():
                if not any(context.pages for context in browser.contexts):
                    raise RuntimeError("No open browser pages remain.")
                sleep(1)

    def stop(self) -> None:
        self.stop_event.set()
