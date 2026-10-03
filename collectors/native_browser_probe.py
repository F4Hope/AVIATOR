"""Launch a local persistent Edge/Chrome session and observe sanitized Aviator traffic."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Event
from time import sleep

from playwright.sync_api import BrowserContext, Page, Response, WebSocket, sync_playwright

from collectors.network_probe import ProbeWriter, payload_metadata, safe_url


logger = logging.getLogger("aie.collectors.native_browser_probe")
SUPPORTED_CHANNELS = ("msedge", "chrome")


@dataclass(frozen=True, slots=True)
class NativeBrowserProbeConfig:
    target_url: str
    output_path: Path
    profile_dir: Path
    browser_channel: str = "msedge"

    def __post_init__(self) -> None:
        if not self.target_url.startswith("https://"):
            raise ValueError("target_url must use https://.")
        if self.browser_channel not in SUPPORTED_CHANNELS:
            raise ValueError("browser_channel must be msedge or chrome.")
        if self.output_path.suffix.lower() != ".jsonl":
            raise ValueError("output_path must end in .jsonl.")


class NativeBrowserProbe:
    """Observe a local persistent browser without saving credentials or raw values."""

    def __init__(self, config: NativeBrowserProbeConfig) -> None:
        self.config = config
        self.writer = ProbeWriter(config.output_path)
        self.stop_event = Event()

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

    def _prepare_page(self, context: BrowserContext) -> Page:
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
        """Launch a visible persistent browser and observe it until stopped."""
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir),
                channel=self.config.browser_channel,
                headless=False,
            )
            try:
                page = self._prepare_page(context)
                logger.info(
                    "Native browser probe running in %s; use the opened browser manually.",
                    self.config.browser_channel,
                )
                while not self.stop_event.is_set():
                    if page.is_closed():
                        open_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                        if not open_pages:
                            raise RuntimeError("Observed browser was closed.")
                        page = open_pages[0]
                    sleep(1)
            finally:
                context.close()

    def stop(self) -> None:
        self.stop_event.set()
