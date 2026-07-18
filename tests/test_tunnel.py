"""Tunnel pointer tests.

The redirect target decides where a bookmark sends the user, so the
security properties matter more than the happy path: anyone who can write
this value can aim the bookmark at a page imitating the dashboard and
collect the password.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.routers import tunnel as tunnel_mod

SECRET = "test-secret"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """Fully isolated: private engine, private app, no global state touched.

    Mounting app.main would run its lifespan (re-init the DB, start the
    scheduler), and calling the global init_db() collides with the other
    suites that rebind the engine to their own temp databases.
    """
    monkeypatch.setattr(tunnel_mod.settings, "tunnel_update_secret", SECRET)

    engine = create_engine(f"sqlite:///{tmp_path/'tunnel.db'}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    isolated = FastAPI()
    isolated.include_router(tunnel_mod.router)
    isolated.dependency_overrides[get_db] = _override_db
    with TestClient(isolated) as c:
        yield c
    engine.dispose()


def test_go_is_503_before_anything_published(client):
    assert client.get("/go", follow_redirects=False).status_code == 503


def test_bad_secret_is_rejected(client):
    r = client.post("/api/tunnel", json={
        "url": "https://abc-def.trycloudflare.com", "secret": "nope"})
    assert r.status_code == 403


def test_missing_secret_config_refuses_writes(client, monkeypatch):
    """An unconfigured deployment must not accept anonymous updates."""
    monkeypatch.setattr(tunnel_mod.settings, "tunnel_update_secret", "")
    r = client.post("/api/tunnel", json={
        "url": "https://abc-def.trycloudflare.com", "secret": ""})
    assert r.status_code == 503


def test_only_allowed_hosts_accepted(client):
    """A leaked secret must still not aim the bookmark anywhere."""
    for bad in (
        "https://evil.com",
        "https://trycloudflare.com.evil.com",   # suffix trick
        "http://abc.trycloudflare.com",          # plain http
    ):
        r = client.post("/api/tunnel", json={"url": bad, "secret": SECRET})
        assert r.status_code == 400, f"accepted {bad}"


def test_publish_then_redirect(client):
    url = "https://ownership-traveling-routers.trycloudflare.com"
    assert client.post(
        "/api/tunnel", json={"url": url, "secret": SECRET}
    ).status_code == 200
    r = client.get("/go", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].rstrip("/") == url


def test_republish_overwrites(client):
    for host in ("https://one-two.trycloudflare.com",
                 "https://three-four.trycloudflare.com"):
        client.post("/api/tunnel", json={"url": host, "secret": SECRET})
    r = client.get("/go", follow_redirects=False)
    assert "three-four" in r.headers["location"]
