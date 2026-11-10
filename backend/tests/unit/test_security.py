import uuid

from pydantic import SecretStr

from app.config import get_settings
from app.services.security import (
    create_access_token,
    hash_password,
    read_access_token,
    verify_password,
)


def test_hash_is_not_the_password_and_verifies():
    hashed = hash_password("correct horse")
    assert hashed != "correct horse"
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong horse", hashed)


def test_same_password_hashes_differently_each_time():
    # A random salt per hash means equal passwords don't produce equal hashes.
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_garbage_hash():
    assert not verify_password("anything", "not-a-hash")


def test_token_round_trip():
    user_id = uuid.uuid4()
    token = create_access_token(user_id, get_settings())
    assert read_access_token(token, get_settings()) == user_id


def test_expired_token_is_rejected():
    expired_settings = get_settings().model_copy(update={"jwt_expire_minutes": -1})
    token = create_access_token(uuid.uuid4(), expired_settings)
    assert read_access_token(token, get_settings()) is None


def test_token_signed_with_another_secret_is_rejected():
    other = get_settings().model_copy(
        update={"jwt_secret": SecretStr("someone-elses-secret-0123456789abcdef")}
    )
    token = create_access_token(uuid.uuid4(), other)
    assert read_access_token(token, get_settings()) is None


def test_malformed_token_is_rejected():
    assert read_access_token("not.a.token", get_settings()) is None
