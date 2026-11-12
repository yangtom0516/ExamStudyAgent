"""Small helpers shared by API tests."""

# Minimal files that pass the "first bytes" check. They aren't real documents; parsing comes in step 2c.
PDF_BYTES = b"%PDF-1.4\n% tiny test file\n"
PPTX_BYTES = b"PK\x03\x04" + b"\x00" * 26


def create_course(client, name="EECS 281") -> dict:
    response = client.post("/courses", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def create_session(client, course_id, name="Midterm 1 prep") -> dict:
    response = client.post(f"/courses/{course_id}/sessions", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, session_id, filename="lecture1.pdf", content=PDF_BYTES, kind=None):
    """POST a file to a session; returns the raw response."""
    data = {"kind": kind} if kind else {}
    return client.post(
        f"/sessions/{session_id}/documents",
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
    )


def upload_ok(client, session_id, **kwargs) -> dict:
    response = upload(client, session_id, **kwargs)
    assert response.status_code == 202, response.text
    return response.json()
