import uuid

import pytest

from tests.helpers import create_course, create_session


def test_courses_require_login(client):
    assert client.get("/courses").status_code == 401
    assert client.post("/courses", json={"name": "EECS 281"}).status_code == 401


def test_create_and_list_courses(client, login_as):
    login_as("alice")
    first = create_course(client, "  EECS 281  ")
    create_course(client, "Math 215")

    assert first["name"] == "EECS 281"  # surrounding spaces trimmed
    uuid.UUID(first["id"])
    assert [c["name"] for c in client.get("/courses").json()] == ["EECS 281", "Math 215"]


def test_get_course(client, login_as):
    login_as("alice")
    course = create_course(client)
    assert client.get(f"/courses/{course['id']}").json() == course


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_course_name_is_validated(client, login_as, name):
    login_as("alice")
    assert client.post("/courses", json={"name": name}).status_code == 422


def test_unknown_course_is_404(client, login_as):
    login_as("alice")
    assert client.get(f"/courses/{uuid.uuid4()}").status_code == 404


def test_users_only_see_their_own_courses(client, login_as):
    login_as("alice")
    alice_course = create_course(client)
    url = f"/courses/{alice_course['id']}"

    login_as("bob")
    assert client.get("/courses").json() == []
    assert client.get(url).status_code == 404  # same answer as a course that doesn't exist
    assert client.delete(url).status_code == 404

    login_as("alice")
    assert client.get(url).status_code == 200  # Bob's delete did nothing


def test_delete_course_also_deletes_its_sessions(client, login_as):
    login_as("alice")
    course = create_course(client)
    session = create_session(client, course["id"])

    assert client.delete(f"/courses/{course['id']}").status_code == 204
    assert client.get(f"/courses/{course['id']}").status_code == 404
    assert client.get(f"/sessions/{session['id']}").status_code == 404
    assert client.get("/courses").json() == []
