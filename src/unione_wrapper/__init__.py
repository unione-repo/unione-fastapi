"""UniOne Web API for FastAPI: client dependency, webhooks.

    webhooks = Webhooks()

    @webhooks.on_event
    async def on_event(event, user): ...

    app.include_router(webhooks.router())

    @app.get("/usage")
    def usage(client: UniOneClient = Depends(get_client)): ...

Settings come from the environment variables UNIONE_*; replace them with
app.dependency_overrides[get_settings] = lambda: Settings(api_key=...).
"""
from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from unione import UniOneClient

from . import webhook as notifications
from ._settings import Settings
from .webhook import WebhookCallback, WebhookCallbackEventsByUserItemEventsItem, WebhookCallbackEventsByUserItem

__all__ = ["Settings", "Webhooks", "get_client", "get_settings"]
__version__ = "1.1.2"


@lru_cache(maxsize=None)
def get_settings() -> Settings:
    """Settings from the environment variables UNIONE_* (dependency, can be overridden)."""
    return Settings.load()


@lru_cache(maxsize=None)
def _client(settings: Settings) -> UniOneClient:
    return UniOneClient(settings.require_key(), settings.host, settings.timeout, platform="fastapi.1.1.2")


def get_client(settings: Settings = Depends(get_settings)) -> UniOneClient:
    """Dependency: the UniOne API client of the settings (one instance per settings).

    The client is synchronous: use it in `def` endpoints (FastAPI runs them in a thread pool).
    """
    return _client(settings)


NotificationHandler = Callable[[WebhookCallback, Dict[str, Any]], Union[None, Awaitable[None]]]
EventHandler = Callable[[WebhookCallbackEventsByUserItemEventsItem, WebhookCallbackEventsByUserItem], Union[None, Awaitable[None]]]


async def _call(handler: Callable[..., Any], *args: Any) -> None:
    value = handler(*args)
    if inspect.isawaitable(value):
        await value


class Webhooks:
    """Handlers of the webhook notifications and the router of the endpoint.

    GET/HEAD -> 200 (URL check of webhook/set), POST -> signature check, then every on_notification handler
    with (callback, payload) and every on_event handler with (event, user) for each event. Handlers may be sync
    or async; keep them fast, the API waits for 200 OK for 3 seconds only.
    """

    def __init__(self) -> None:
        self.notification_handlers: List[NotificationHandler] = []
        self.event_handlers: List[EventHandler] = []

    def on_notification(self, handler: NotificationHandler) -> NotificationHandler:
        self.notification_handlers.append(handler)
        return handler

    def on_event(self, handler: EventHandler) -> EventHandler:
        self.event_handlers.append(handler)
        return handler

    async def handle(self, body: bytes, settings: Settings) -> PlainTextResponse:
        try:
            callback, payload = notifications.parse(body, settings.signing_key)
        except notifications.WebhookError as e:
            return PlainTextResponse(str(e), status_code=e.status)
        for handler in self.notification_handlers:
            await _call(handler, callback, payload)
        for event, user in notifications.events(callback):
            for event_handler in self.event_handlers:
                await _call(event_handler, event, user)
        return PlainTextResponse("OK")

    def router(self, path: Optional[str] = None, **kwargs: Any) -> APIRouter:
        """Router with the endpoint at path (default: UNIONE_WEBHOOK_PATH or /unione/webhook); kwargs go to APIRouter."""
        router = APIRouter(**kwargs)

        async def webhook(request: Request, settings: Settings = Depends(get_settings)) -> PlainTextResponse:
            if request.method != "POST":
                return PlainTextResponse("OK")
            return await self.handle(await request.body(), settings)

        router.add_api_route(path or get_settings().webhook_path, webhook, methods=["GET", "HEAD", "POST"],
                             include_in_schema=False, name="unione_webhook")
        return router
