import os

os.environ["DATABASE_URL"] = "sqlite://"  # in-memory, single connection
# Pin the platform identity so tests are hermetic — never dependent on config
# defaults or a developer's local .env. `.test` is reserved (RFC 2606), so nothing
# here can ever resolve or leak onto the network.
os.environ["BASE_DOMAIN"] = "warpyard.test"
os.environ["PUBLIC_URL"] = "https://app.warpyard.test"
os.environ["MCP_URL"] = "https://mcp.warpyard.test"
os.environ["EDGE_HOST"] = "edge.warpyard.test"
os.environ["EDGE_IP"] = "203.0.113.10"  # TEST-NET-3, never routable
os.environ["PROXMOX_API_URL"] = "https://pve.warpyard.test:8006"
os.environ["PBS_API_URL"] = "https://pbs.warpyard.test:8007"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database
from app.database import Base
from app.models import Image, Plan, User


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    session = TestSession()
    yield session
    session.close()


@pytest.fixture()
def seeded(db):
    user = User(email="friend@example.com", max_instances=2, max_vcpus=4, max_disk_gb=80)
    plan = Plan(
        slug="wy-1-1", name="Warpyard 1GB", vcpus=1, memory_mb=1024, disk_gb=20, transfer_gb=500, price_cents=400
    )
    big_plan = Plan(
        slug="wy-4-8", name="Warpyard 8GB", vcpus=4, memory_mb=8192, disk_gb=80, transfer_gb=2000, price_cents=1600
    )
    image = Image(slug="ubuntu-24.04", name="Ubuntu 24.04 LTS", distro="ubuntu", version="24.04", template_vmid=9000)
    db.add_all([user, plan, big_plan, image])
    db.commit()
    return {"user": user, "plan": plan, "big_plan": big_plan, "image": image}


@pytest.fixture()
def client(db):
    from app.database import get_db
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_db] = lambda: db
    # https base: the session cookie is Secure-only now, so an http test origin would drop it
    yield TestClient(fastapi_app, base_url="https://testserver")
    fastapi_app.dependency_overrides.clear()
