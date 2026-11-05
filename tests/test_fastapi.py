"""Tests of the FastAPI integration: client dependency, settings, webhook router. No network: the HTTP layer of
the SDK is faked (fake_api.py), TestClient calls the app in process.

    pip install -e ".[test]" && python -m unittest discover -s tests -v
"""
import os
import unittest
import warnings

from fastapi import Depends, FastAPI

with warnings.catch_warnings():  # starlette may warn about the httpx version
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from fake_api import API_KEY, BASE_PATH, BASE_URL, KEY_HEADER, PLATFORM, WEBHOOK_PATH, FakeApi, clean_env, signed
from unione import UniOneClient
from unione_wrapper import Settings, Webhooks, get_client, get_settings
from unione_wrapper.webhook import WebhookCallback

SETTINGS = Settings(api_key=API_KEY, host=BASE_URL)


def make_app(settings=SETTINGS, path=WEBHOOK_PATH):
    """An app with the webhook router and a /ping route on the client dependency; received: what the handlers got."""
    app = FastAPI()
    webhooks = Webhooks()
    received = []

    @webhooks.on_notification
    async def on_notification(callback, payload):
        received.append({"callback": callback, "payload": payload})

    @webhooks.on_event
    def on_event(event, user):
        received.append({"event": event, "user": user})

    app.include_router(webhooks.router(path))

    @app.get("/ping")
    def ping(client: UniOneClient = Depends(get_client)):
        client.system.ping()
        return {"client": id(client)}

    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app), received


class Base(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        self.api = FakeApi(self)


class ClientTest(Base):
    def test_client_dependency(self):
        http, _ = make_app()
        first, second = http.get("/ping"), http.get("/ping")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json(), second.json())  # one client per settings
        self.assertEqual(len(self.api.calls), 2)
        call = self.api.calls[0]
        self.assertEqual(call.path, BASE_PATH + "/system/ping.json")
        self.assertEqual(call.headers[KEY_HEADER], API_KEY)
        self.assertEqual(call.platform, PLATFORM)

    def test_settings_from_environment(self):
        os.environ["UNIONE_API_KEY"] = "env-key"
        os.environ["UNIONE_HOST"] = BASE_URL
        os.environ["UNIONE_TIMEOUT"] = "5"
        settings = get_settings()
        self.assertEqual((settings.api_key, settings.host, settings.timeout), ("env-key", BASE_URL, 5.0))
        self.assertEqual(get_client(settings).api_client.config.base_url, BASE_URL)

    def test_missing_key(self):
        with self.assertRaises(RuntimeError):
            get_client(Settings(host=BASE_URL))


class WebhookTest(Base):
    def test_get(self):
        http, _ = make_app()
        self.assertEqual(http.get(WEBHOOK_PATH).status_code, 200)

    def test_signed_post(self):
        http, received = make_app()
        response = http.post(WEBHOOK_PATH, content=signed(), headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(received), 3)
        self.assertIsInstance(received[0]["callback"], WebhookCallback)
        self.assertEqual(received[1]["event"].event_data.email, "user@example.com")
        self.assertEqual(self.api.calls, [])

    def test_wrong_signature(self):
        http, received = make_app()
        body = signed().replace(b"user@", b"evil@")
        self.assertEqual(http.post(WEBHOOK_PATH, content=body).status_code, 401)
        self.assertEqual(received, [])

    def test_webhook_key(self):
        http, _ = make_app(Settings(api_key=API_KEY, host=BASE_URL, webhook_key="hook-key"))
        self.assertEqual(http.post(WEBHOOK_PATH, content=signed("hook-key")).status_code, 200)
        self.assertEqual(http.post(WEBHOOK_PATH, content=signed()).status_code, 401)

    def test_default_path_from_environment(self):
        os.environ["UNIONE_WEBHOOK_PATH"] = "/hooks/mail"
        http, _ = make_app(path=None)
        self.assertEqual(http.get("/hooks/mail").status_code, 200)
        self.assertEqual(http.get(WEBHOOK_PATH).status_code, 404)

    def test_router_not_included(self):
        http = TestClient(FastAPI())
        self.assertEqual(http.post(WEBHOOK_PATH, content=signed()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
