import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_fastapi_testclient_uses_supported_httpx2_backend() -> None:
    application = FastAPI()

    @application.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(application) as client:
        response = client.get("/ping")

    assert isinstance(client, httpx2.Client)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
