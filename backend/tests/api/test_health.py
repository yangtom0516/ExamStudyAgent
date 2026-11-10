def test_health_reports_all_stores_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "redis": "ok", "qdrant": "ok", "storage": "ok"},
    }
