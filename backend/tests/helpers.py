"""Small helpers shared by API tests."""


def create_course(client, name="EECS 281") -> dict:
    response = client.post("/courses", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def create_session(client, course_id, name="Midterm 1 prep") -> dict:
    response = client.post(f"/courses/{course_id}/sessions", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()
