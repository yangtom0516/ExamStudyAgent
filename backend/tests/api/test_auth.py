import uuid

import pytest

from app.api.deps import AUTH_COOKIE
from app.config import get_settings
from app.services.security import create_access_token

PASSWORD = "password123"


def register(client, username="Tom_Test", password=PASSWORD):
    return client.post("/auth/register", json={"username": username, "password": password})


def login(client, username="tom_test", password=PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


# --- register ---------------------------------------------------------------


def test_register_creates_user_and_logs_in(client):
    response = register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "tom_test"  # stored lowercase
    uuid.UUID(body["id"])

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{AUTH_COOKIE}=")
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()

    assert client.get("/auth/me").json() == body


def test_password_is_stored_hashed(client, db_fetch_one):
    register(client)
    row = db_fetch_one("SELECT password_hash FROM users WHERE username = :u", u="tom_test")
    assert row["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in row["password_hash"]


def test_register_duplicate_username_is_rejected_case_insensitively(client):
    register(client, username="tom_test")
    response = register(client, username="TOM_TEST")
    assert response.status_code == 409
    assert response.json()["detail"] == "Username already taken"


@pytest.mark.parametrize(
    "username, password",
    [
        ("ab", PASSWORD),  # too short
        ("a" * 31, PASSWORD),  # too long
        ("tom!", PASSWORD),  # bad character
        ("tom test", PASSWORD),  # space
        ("tom_test", "short"),  # password under 8 characters
    ],
)
def test_register_validates_input(client, username, password):
    assert register(client, username=username, password=password).status_code == 422


# --- login / logout ---------------------------------------------------------


def test_login_with_correct_password(client):
    register(client)
    client.cookies.clear()  # act like a different browser

    response = login(client, username="TOM_test")  # any capitalization works
    assert response.status_code == 200
    assert response.json()["username"] == "tom_test"
    assert client.get("/auth/me").status_code == 200


def test_login_wrong_password_and_unknown_user_look_the_same(client):
    register(client)
    client.cookies.clear()

    wrong_password = login(client, password="wrongpass")
    unknown_user = login(client, username="nobody")

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json() == {"detail": "Invalid username or password"}
    assert AUTH_COOKIE not in client.cookies


def test_logout_clears_cookie(client):
    register(client)

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


# --- /auth/me with bad cookies ----------------------------------------------


def test_me_without_cookie_is_401(client):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Not logged in"


def test_me_with_forged_cookie_is_401(client):
    client.cookies.set(AUTH_COOKIE, "forged.token.value")
    assert client.get("/auth/me").status_code == 401


def test_me_with_expired_cookie_is_401(client):
    user_id = uuid.UUID(register(client).json()["id"])
    expired = create_access_token(user_id, get_settings().model_copy(update={"jwt_expire_minutes": -1}))
    client.cookies.clear()  # drop the valid cookie from registering
    client.cookies.set(AUTH_COOKIE, expired)
    assert client.get("/auth/me").status_code == 401


def test_me_for_deleted_user_is_401(client):
    # A validly signed token for a user that doesn't exist (e.g. account deleted).
    client.cookies.set(AUTH_COOKIE, create_access_token(uuid.uuid4(), get_settings()))
    assert client.get("/auth/me").status_code == 401
