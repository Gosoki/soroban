"""登录 / JWT / 改密码。"""
import datetime as dt

import pytest
from jose import jwt

from app.config import settings


def test_login_ok_and_me(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


def test_login_bad_password(anon):
    r = anon.post("/api/auth/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user(anon):
    r = anon.post("/api/auth/login", data={"username": "nobody", "password": "x"})
    assert r.status_code == 401


@pytest.mark.parametrize("path", [
    "/api/orders", "/api/shipment", "/api/misc", "/api/items", "/api/staging",
    "/api/dashboard", "/api/fx", "/api/tags/platform_account", "/api/plugins", "/api/db/status",
])
def test_all_routes_require_auth(anon, path):
    assert anon.get(path).status_code == 401


def test_token_tampering_rejected(anon, token):
    # 改一个字符即签名失效
    bad = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    r = anon.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_token_signed_with_other_key_rejected(anon):
    payload = {"sub": "1", "username": "admin",
               "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)}
    forged = jwt.encode(payload, "some-other-key", algorithm=settings.ALGORITHM)
    r = anon.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_expired_token_rejected(anon):
    payload = {"sub": "1", "username": "admin",
               "exp": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)}
    expired = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    r = anon.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_token_without_sub_rejected(anon):
    payload = {"username": "admin", "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)}
    t = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    assert anon.get("/api/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_change_password_wrong_old(client):
    r = client.post("/api/auth/change-password",
                    json={"old_password": "not-it", "new_password": "abcdef"})
    assert r.status_code == 400


def test_change_password_too_long_rejected(client):
    # bcrypt 只吃 72 字节，schema 应挡住超长
    r = client.post("/api/auth/change-password",
                    json={"old_password": "admin12345", "new_password": "汉" * 30})
    assert r.status_code == 422


def test_change_password_too_short_rejected(client):
    r = client.post("/api/auth/change-password",
                    json={"old_password": "admin12345", "new_password": "abc"})
    assert r.status_code == 422
