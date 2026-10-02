"""Attach to a Browserless Chromium session and observe sanitized Aviator traffic."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Event
from time import sleep
from urllib.parse import urlencode

from playwright.sync_api import Browser, Page, Response, WebSocket, sync_playwright

from collectors.network_probe import ProbeWriter, payload_metadata, safe_url


logger = logging.getLogger("aie.collectors.browser_probe")


@dataclass(frozen=True, slots=True)
class BrowserProbeConfig:
    ws_origin: str
    token: str
    target_url: str
    output_path: Path

    def __post_init__(self) -> None:
        if not self.ws_origin.startswith(("ws://", "wss://")):
            raise ValueError("ws_origin must use ws:// or wss://.")
        if not self.token or any(character.isspace() for character in self.token):
            raise ValueError("Browserless token is required.")
        if not self.target_url.startswith("https://"):
            raise ValueError("target_url must use https://.")


class BrowserProbe:
    """Observe browser traffic without persisting cookies, headers, query strings, or raw payloads."""

    def __init__(self, config: BrowserProbeConfig) -> None:
        self.config = config
        self.writer = ProbeWriter(config.output_path)
        self.stop_event = Event()

    def _endpoint(self) -> str:
        separator = "&" if "?" in self.config.ws_origin else "?"
        return self.config.ws_origin + separator + urlencode({"token": self.config.token})

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

    def _prepare_page(self, browser: Browser) -> Page:
        if not browser.contexts:
            raise RuntimeError("Browserless returned no default browser context.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", self._record_response)
        page.on("websocket", self._record_websocket)
        page.goto(self.config.target_url, wait_until="domcontentloaded", timeout=60_000)
        self.writer.append({
            "kind": "page_ready",
            "url": safe_url(page.url),
            "title_length": len(page.title()),
        })
        return page

    def run(self) -> None:
        """Keep one Browserless connection alive until stopped or disconnected."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self._endpoint(), timeout=60_000)
            try:
                page = self._prepare_page(browser)
                logger.info("Browser probe connected; observing page traffic.")
                while not self.stop_event.is_set():
                    if page.is_closed():
                        raise RuntimeError("Observed browser page was closed.")
                    sleep(1)
            finally:
                browser.close()

    def stop(self) -> None:
        self.stop_event.set()
