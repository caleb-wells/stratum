"""One-time setup of the chat frontend. Safe to run again.

Creates the admin account (the first account on a fresh install), installs and
enables the Pipe function, and creates one ordinary account for the guest.
Self-registration stays disabled throughout.

Run this BEFORE the site is reachable from the internet (the deploy script starts the
HTTPS proxy only after this succeeds): on a fresh install the first account to register
becomes the admin, so that first registration must be ours.

    python -m app.bootstrap_webui
Environment: WEBUI_INTERNAL_URL, ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_NAME,
             DEMO_EMAIL, DEMO_PASSWORD, DEMO_NAME (the DEMO_* ones are optional)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

BASE = os.getenv("WEBUI_INTERNAL_URL", "http://open-webui:8080").rstrip("/")
PIPE_ID = "jurisdiction_rag"
PIPE_FILE = Path(os.getenv("PIPE_FILE", "/srv/services/openwebui/jurisdiction_rag_pipe.py"))


def wait_until_up(timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE}/health", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    sys.exit("chat frontend did not become healthy")


def sign_in(email: str, password: str) -> str | None:
    r = requests.post(f"{BASE}/api/v1/auths/signin", json={"email": email, "password": password}, timeout=30)
    return r.json().get("token") if r.ok else None


def main() -> None:
    admin_email, admin_password = os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"]
    wait_until_up()

    token = sign_in(admin_email, admin_password)
    if not token:
        r = requests.post(f"{BASE}/api/v1/auths/signup", timeout=30, json={
            "name": os.getenv("ADMIN_NAME", "Admin"), "email": admin_email, "password": admin_password})
        if not r.ok:
            sys.exit("could not create the admin account. If accounts already exist, someone else "
                     f"registered first: wipe the frontend's data volume and start again. "
                     f"({r.status_code} {r.text[:200]})")
        token = r.json()["token"]
        if r.json().get("role") != "admin":
            sys.exit("the account was created but is NOT the admin: another account already existed. "
                     "Wipe the frontend's data volume and start again before exposing the site.")
        print("admin account created")
    auth = {"Authorization": f"Bearer {token}"}

    form = {"id": PIPE_ID, "name": "Stratum", "content": PIPE_FILE.read_text(),
            "meta": {"description": "Jurisdiction-scoped retrieval with cited sources.", "manifest": {}}}
    existing = requests.get(f"{BASE}/api/v1/functions/id/{PIPE_ID}", headers=auth, timeout=30)
    if existing.ok and existing.json():
        r = requests.post(f"{BASE}/api/v1/functions/id/{PIPE_ID}/update", headers=auth, json=form, timeout=60)
        active = r.ok and r.json().get("is_active")
    else:
        r = requests.post(f"{BASE}/api/v1/functions/create", headers=auth, json=form, timeout=60)
        active = False
    if not r.ok:
        sys.exit(f"could not install the pipe function: {r.status_code} {r.text[:300]}")
    if not active:
        t = requests.post(f"{BASE}/api/v1/functions/id/{PIPE_ID}/toggle", headers=auth, timeout=30)
        if not (t.ok and t.json().get("is_active")):
            sys.exit(f"could not enable the pipe function: {t.status_code} {t.text[:200]}")
    print("pipe function installed and enabled")

    demo_email, demo_password = os.getenv("DEMO_EMAIL"), os.getenv("DEMO_PASSWORD")
    if demo_email and demo_password:
        if sign_in(demo_email, demo_password):
            print("guest account already exists")
        else:
            r = requests.post(f"{BASE}/api/v1/auths/add", headers=auth, timeout=30, json={
                "name": os.getenv("DEMO_NAME", "Guest"), "email": demo_email,
                "password": demo_password, "role": "user"})
            print("guest account created" if r.ok else f"guest account NOT created: {r.status_code} {r.text[:200]}")

    models = requests.get(f"{BASE}/api/models", headers=auth, timeout=60)
    names = [m["name"] for m in models.json().get("data", [])] if models.ok else []
    print("models visible:", names or "none (check the function)")


if __name__ == "__main__":
    main()
