from test_workflow import setup, upload, xlsx
from sqlalchemy import select
from app.main import create_app
from app.models import User


def test_bootstrap_name_is_used_only_for_first_account(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "StrongPassword123!")
    monkeypatch.setenv("ADMIN_NAME", "  Rodrigo D'Ávila  ")
    url = "sqlite:///" + str(tmp_path / "name.db")
    app = create_app(url)
    with app.state.factory() as db:
        assert db.scalar(select(User)).name == "Rodrigo D'Ávila"
    monkeypatch.setenv("ADMIN_NAME", "Replacement Name")
    app = create_app(url)
    with app.state.factory() as db:
        assert db.scalar(select(User)).name == "Rodrigo D'Ávila"


def test_preview_contains_every_record_and_preserves_data(setup):
    client, headers, contract, app = setup
    ids = tuple(f"STATION-{i:04d}" for i in range(75))
    response = upload(client, headers, contract["id"], data=xlsx(ids))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["count"] == len(result["records"]) == 75
    assert [r["external_id"] for r in result["records"]] == list(ids)
    assert len(result["sample"]) == 10  # Existing client compatibility.
    assert result["records"][-1]["details"]["source"]["Custom"] == "extra"
    assert client.get("/api/contracts/" + contract["id"] + "/chargers", headers=headers).json() == []
