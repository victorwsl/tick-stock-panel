from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import personal_research_management as api
from app.services import research_metadata as service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "data_dir", tmp_path)
    app = FastAPI()
    app.state.strategy_engine = SimpleNamespace(
        list_strategies=lambda **kw: [{"id": sid} for sid in ("a", "b", "c")])
    app.include_router(api.router)
    with TestClient(app) as value:
        yield value


def test_persistence_partial_updates_and_registry_filter(client, tmp_path):
    assert client.get("/api/research/strategies").json() == {"items": {}}
    response = client.patch("/api/research/strategies/a", json={"category": "trend", "favorite": True})
    assert response.status_code == 200
    assert response.json() == {"id": "a", "category": "trend", "favorite": True,
                               "note": "", "label": "", "parent_id": None}
    response = client.patch("/api/research/strategies/b", json={"parent_id": "a", "label": "分支"})
    assert response.status_code == 200
    response = client.patch("/api/research/strategies/a", json={"note": "对照"})
    assert response.json()["category"] == "trend"
    assert service.list_strategy_metadata(tmp_path, {"a"})["a"]["note"] == "对照"
    assert "b" not in service.list_strategy_metadata(tmp_path, {"a"})
    assert client.patch("/api/research/strategies/b", json={"parent_id": None}).json()["parent_id"] is None


@pytest.mark.parametrize("sid, patch, status", [
    ("missing", {}, 404), ("a", {"parent_id": "missing"}, 422),
    ("a", {"parent_id": "a"}, 422), ("a", {"parent_id": "../bad"}, 422),
    ("a", {"category": "bad"}, 422), ("a", {"note": "x" * 4001}, 422),
    ("a", {"favorite": "yes"}, 422), ("a", {"label": "x" * 121}, 422),
    ("a", {"arbitrary": "value"}, 422),
])
def test_validation(client, sid, patch, status):
    assert client.patch("/api/research/strategies/" + sid, json=patch).status_code == status


def test_cycle_rejected_without_mutation(client):
    assert client.patch("/api/research/strategies/a", json={"parent_id": "b"}).status_code == 200
    assert client.patch("/api/research/strategies/b", json={"parent_id": "c"}).status_code == 200
    assert client.patch("/api/research/strategies/c", json={"parent_id": "a"}).status_code == 422
    rows = client.get("/api/research/strategies").json()["items"]
    assert "c" not in rows


def test_corruption_and_save_failures_visible(client, tmp_path, monkeypatch):
    path = tmp_path / "research_metadata" / "strategies.json"
    path.parent.mkdir()
    path.write_text("{")
    assert client.get("/api/research/strategies").status_code == 500
    assert client.patch("/api/research/strategies/a", json={"note": "new"}).status_code == 500
    assert path.read_text() == "{"
    path.write_text("{}")

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(service.os, "replace", fail)
    assert client.patch("/api/research/strategies/a", json={"note": "new"}).status_code == 500
    assert path.read_text() == "{}"
    assert not list(path.parent.glob(".metadata-*"))


def test_concurrent_strategy_edits_do_not_lose_annotations(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(service.patch_strategy_metadata, tmp_path, sid,
                               service.StrategyMetadataPatch(**patch), {"a", "b"})
                   for sid, patch in [("a", {"note": "a-note"}), ("a", {"favorite": True}),
                                      ("b", {"label": "b-label"})] * 8]
        for future in futures:
            future.result()
    rows = service.list_strategy_metadata(tmp_path, {"a", "b"})
    assert rows["a"]["note"] == "a-note"
    assert rows["a"]["favorite"] is True
    assert rows["b"]["label"] == "b-label"


def test_symlink_metadata_not_read_or_overwritten(client, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    folder = tmp_path / "research_metadata"
    folder.mkdir()
    (folder / "strategies.json").symlink_to(outside)
    assert client.get("/api/research/strategies").status_code == 500
    assert client.patch("/api/research/strategies/a", json={"favorite": True}).status_code == 500
    assert outside.read_text() == "{}"
