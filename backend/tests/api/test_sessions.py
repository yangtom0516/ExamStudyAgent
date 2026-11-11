import uuid

import pytest

from tests.helpers import create_course, create_session


def test_new_session_starts_empty_in_setup(client, login_as):
    login_as("alice")
    course = create_course(client)
    session = create_session(client, course["id"], "  Midterm 1 prep ")

    assert session["name"] == "Midterm 1 prep"
    assert session["course_id"] == course["id"]
    assert session["status"] == "setup"
    assert session["started_at"] is None


def test_list_sessions_only_shows_that_course(client, login_as):
    login_as("alice")
    eecs = create_course(client, "EECS 281")
    math = create_course(client, "Math 215")
    create_session(client, eecs["id"], "Midterm 1")
    create_session(client, eecs["id"], "Final")
    create_session(client, math["id"], "Quiz 3")

    names = [s["name"] for s in client.get(f"/courses/{eecs['id']}/sessions").json()]
    assert names == ["Midterm 1", "Final"]


def test_get_and_delete_session(client, login_as):
    login_as("alice")
    course = create_course(client)
    session = create_session(client, course["id"])
    url = f"/sessions/{session['id']}"

    assert client.get(url).json() == session
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert client.get(f"/courses/{course['id']}").status_code == 200  # the course stays


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_session_name_is_validated(client, login_as, name):
    login_as("alice")
    course = create_course(client)
    assert client.post(f"/courses/{course['id']}/sessions", json={"name": name}).status_code == 422


def test_unknown_session_is_404(client, login_as):
    login_as("alice")
    assert client.get(f"/sessions/{uuid.uuid4()}").status_code == 404


def test_users_cannot_touch_each_others_sessions(client, login_as):
    login_as("alice")
    course = create_course(client)
    session = create_session(client, course["id"])

    login_as("bob")
    assert client.get(f"/courses/{course['id']}/sessions").status_code == 404
    assert client.post(f"/courses/{course['id']}/sessions", json={"name": "sneaky"}).status_code == 404
    assert client.get(f"/sessions/{session['id']}").status_code == 404
    assert client.delete(f"/sessions/{session['id']}").status_code == 404

    login_as("alice")
    assert [s["id"] for s in client.get(f"/courses/{course['id']}/sessions").json()] == [session["id"]]
