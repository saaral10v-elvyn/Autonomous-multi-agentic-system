"""Auth API integration tests against the configured PostgreSQL database."""

from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.conftest import WEB_ORIGIN


def test_signup_login_me_logout_and_bad_password(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    email = test_emails()
    headers = {"Origin": WEB_ORIGIN}
    signup = api_client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "safe-password-1"}, headers=headers
    )
    assert signup.status_code == 201
    cookie = signup.cookies.get("forge_session")
    assert cookie is not None
    set_cookie = signup.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert api_client.get("/api/v1/me").json()["email"] == email

    api_client.post("/api/v1/auth/logout", headers=headers)
    assert api_client.get("/api/v1/me").status_code == 401
    api_client.cookies.set("forge_session", cookie)
    assert api_client.get("/api/v1/me").status_code == 401

    bad_login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}, headers=headers
    )
    assert bad_login.status_code == 401
    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": "safe-password-1"}, headers=headers
    )
    assert login.status_code == 200
    assert login.cookies.get("forge_session")


def test_duplicate_email_and_csrf_rejection(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    email = test_emails()
    payload = {"email": email, "password": "safe-password-1"}
    assert (
        api_client.post(
            "/api/v1/auth/signup", json=payload, headers={"Origin": WEB_ORIGIN}
        ).status_code
        == 201
    )
    duplicate = api_client.post("/api/v1/auth/signup", json=payload, headers={"Origin": WEB_ORIGIN})
    assert duplicate.status_code == 409
    rejected = api_client.post("/api/v1/auth/logout")
    assert rejected.status_code == 403
    mismatched = api_client.post(
        "/api/v1/auth/logout", headers={"Origin": "https://attacker.example"}
    )
    assert mismatched.status_code == 403
