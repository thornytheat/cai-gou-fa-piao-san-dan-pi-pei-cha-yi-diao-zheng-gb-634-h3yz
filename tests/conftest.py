"""测试固定装置：临时库、固定样例目录、一次完整导入。

约定：不读 datetime.now() 做期望值；金额断言一律用 Decimal 字符串比较。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config                                   # noqa: E402
from app.main import create_app                          # noqa: E402
from app.modules.m1_ingest import service as ingest      # noqa: E402
from app.store import Store                              # noqa: E402

EVAL_DATE = dt.date(2026, 11, 15)


@pytest.fixture()
def settings(tmp_path) -> config.Settings:
    return config.Settings(db_path=str(tmp_path / "invoice3way.sqlite"),
                           sample_dir=str(ROOT / "sample_data"),
                           export_dir=str(tmp_path / "export"),
                           eval_date=EVAL_DATE)


@pytest.fixture()
def store(settings) -> Store:
    return Store(settings.db_path)


@pytest.fixture()
def run(settings, store) -> dict:
    return ingest.run_import(settings=settings, store=store)


@pytest.fixture()
def batch_id(run) -> str:
    return run["batch"]["batch_id"]


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def client_with_run(client) -> TestClient:
    response = client.post("/api/import", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    return client


@pytest.fixture()
def sample_dir() -> pathlib.Path:
    return ROOT / "sample_data"
