from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import db
from app.main import app
from app.models import Base
from app.routers import auth as auth_router

DATASET = Path(__file__).parents[2] / "InvoiceIQ_test_invoices"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[db.get_db] = override_get_db
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", session_factory)
    monkeypatch.setattr("app.settings.settings.upload_dir", str(tmp_path / "uploads"))
    auth_router._failed_logins.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


Register = Callable[..., dict[str, str]]


@pytest.fixture
def register(client: TestClient) -> Register:
    def _register(email: str = "admin@example.com", organization: str = "Northstar Finance") -> dict[str, str]:
        response = client.post(
            "/api/auth/register",
            json={"email": email, "password": PASSWORD, "name": "Test User", "organization_name": organization},
        )
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _register


@pytest.fixture
def auth(register: Register) -> dict[str, str]:
    return register()


def upload(client: TestClient, headers: dict[str, str], *names: str) -> list[dict]:
    files = [("files", (name, (DATASET / name).read_bytes(), "application/pdf")) for name in names]
    response = client.post("/api/invoices/upload", files=files, headers=headers)
    assert response.status_code == 201, response.text
    return [item["invoice"] for item in response.json()["files"]]
