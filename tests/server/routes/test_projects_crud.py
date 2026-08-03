"""Tests for the projects CRUD routes (``/v1/projects``).

The projects router is only mounted when ``create_app`` receives a
``project_store``. The standard conftest ``app`` fixture does not supply one, so
these tests build their own app/client that include it.

Two auth setups are exercised:

- **Single-user** (``project_client``) — no auth provider, so the owner scope is
  the reserved ``None``. This is the OSS / local default.
- **Multi-user** (``multi_user_client`` + ``as_user``) — header auth
  (``UnifiedAuthProvider(source="header")``), so each request's owner is the
  ``X-Forwarded-Email`` identity. Used to prove projects are owner-private: one
  user can never see or mutate another's projects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def project_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Build a FastAPI app that includes the project store."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        project_store=SqlAlchemyProjectStore(db_uri),
    )


@pytest_asyncio.fixture()
async def project_client(
    project_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the project-enabled app."""
    transport = httpx.ASGITransport(app=project_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_project(project_client: httpx.AsyncClient) -> None:
    """Creating a project returns the project object."""
    resp = await project_client.post("/v1/projects", json={"name": "My Project"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "My Project"
    assert body["object"] == "project"
    assert len(body["id"]) == 32
    assert body["updated_at"] is None


async def test_create_trims_and_rejects_empty(project_client: httpx.AsyncClient) -> None:
    """Names are trimmed; empty/whitespace-only names are rejected with 422."""
    resp = await project_client.post("/v1/projects", json={"name": "  Padded  "})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Padded"

    resp = await project_client.post("/v1/projects", json={"name": "   "})
    assert resp.status_code == 422


async def test_create_duplicate_name_conflicts(project_client: httpx.AsyncClient) -> None:
    """Two projects with the same name for one owner returns 409."""
    await project_client.post("/v1/projects", json={"name": "dup"})
    resp = await project_client.post("/v1/projects", json={"name": "dup"})
    assert resp.status_code == 409


async def test_list_projects(project_client: httpx.AsyncClient) -> None:
    """Listing returns the created projects."""
    await project_client.post("/v1/projects", json={"name": "A"})
    await project_client.post("/v1/projects", json={"name": "B"})
    resp = await project_client.get("/v1/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert {p["name"] for p in body["data"]} == {"A", "B"}


async def test_get_project(project_client: httpx.AsyncClient) -> None:
    """A created project can be fetched by id; unknown ids 404."""
    created = (await project_client.post("/v1/projects", json={"name": "X"})).json()
    resp = await project_client.get(f"/v1/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "X"

    missing = await project_client.get(f"/v1/projects/{'0' * 32}")
    assert missing.status_code == 404


async def test_rename_project(project_client: httpx.AsyncClient) -> None:
    """PATCH renames the project and stamps ``updated_at``."""
    created = (await project_client.post("/v1/projects", json={"name": "Old"})).json()
    resp = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "New"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New"
    assert body["updated_at"] is not None


async def test_rename_missing_project_404(project_client: httpx.AsyncClient) -> None:
    """Renaming an unknown project returns 404."""
    resp = await project_client.patch(f"/v1/projects/{'0' * 32}", json={"name": "X"})
    assert resp.status_code == 404


async def test_create_defaults_to_empty_config(project_client: httpx.AsyncClient) -> None:
    """A project created without a config field reports an empty config."""
    body = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    assert body["config"] == {}


async def test_create_and_get_roundtrips_config(project_client: httpx.AsyncClient) -> None:
    """A config passed on create round-trips through the create + get responses."""
    cfg = {"host_id": "host_abc", "workspace": "/work/repo", "model": "claude-opus-4-8"}
    created = (
        await project_client.post("/v1/projects", json={"name": "Configured", "config": cfg})
    ).json()
    assert created["config"] == cfg
    fetched = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert fetched["config"] == cfg


async def test_patch_replaces_config(project_client: httpx.AsyncClient) -> None:
    """PATCH with a new config replaces the stored one and stamps updated_at."""
    created = (
        await project_client.post("/v1/projects", json={"name": "P", "config": {"host_id": "old"}})
    ).json()
    resp = await project_client.patch(
        f"/v1/projects/{created['id']}", json={"config": {"host_id": "new", "model": "m"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"] == {"host_id": "new", "model": "m"}
    assert body["updated_at"] is not None


async def test_patch_without_config_preserves_it(project_client: httpx.AsyncClient) -> None:
    """A PATCH that omits config (e.g. a rename) leaves the stored config intact."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"host_id": "keep"}}
        )
    ).json()
    renamed = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "P2"})
    assert renamed.json()["config"] == {"host_id": "keep"}


async def test_patch_empty_config_clears_it(project_client: httpx.AsyncClient) -> None:
    """PATCH with config={} clears the stored defaults (distinct from omitting)."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"host_id": "drop"}}
        )
    ).json()
    resp = await project_client.patch(f"/v1/projects/{created['id']}", json={"config": {}})
    assert resp.json()["config"] == {}


async def test_delete_project(project_client: httpx.AsyncClient) -> None:
    """DELETE removes the project; a second delete 404s."""
    created = (await project_client.post("/v1/projects", json={"name": "Doomed"})).json()
    resp = await project_client.delete(f"/v1/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    second_delete = await project_client.delete(f"/v1/projects/{created['id']}")
    assert second_delete.status_code == 404


async def test_session_projects_unions_first_class_and_labels(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/sessions/projects dual-reads: first-class projects (with id,
    even when empty) unioned with label-only projects (id=None), by name."""
    conv_store = SqlAlchemyConversationStore(db_uri)

    # An empty first-class project — invisible to the label path, must appear.
    empty = (await project_client.post("/v1/projects", json={"name": "Empty FC"})).json()
    # A name that exists BOTH as first-class and as a label — one entry, fc id.
    both = (await project_client.post("/v1/projects", json={"name": "Both"})).json()
    conv_both = conv_store.create_conversation()
    conv_store.set_labels(conv_both.id, {"omni_project": "Both"})
    # A label-only project — no first-class row, id=None.
    conv_label = conv_store.create_conversation()
    conv_store.set_labels(conv_label.id, {"omni_project": "Label Only"})

    resp = await project_client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": both["id"], "name": "Both"},
        {"id": empty["id"], "name": "Empty FC"},
        {"id": None, "name": "Label Only"},
    ]


# ── Multi-user: projects are owner-private ─────────────────────────────


@pytest.fixture()
def multi_user_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """A project-enabled app with header auth, so each request has an owner."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
    )


@pytest_asyncio.fixture()
async def multi_user_client(
    multi_user_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the multi-user (header-auth) app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_scoped_to_requesting_owner(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Each user sees only their own projects."""
    await multi_user_client.post("/v1/projects", json={"name": "Alice A"}, headers=_as_user(ALICE))
    await multi_user_client.post("/v1/projects", json={"name": "Bob B"}, headers=_as_user(BOB))

    alice = (await multi_user_client.get("/v1/projects", headers=_as_user(ALICE))).json()
    bob = (await multi_user_client.get("/v1/projects", headers=_as_user(BOB))).json()
    assert {p["name"] for p in alice["data"]} == {"Alice A"}
    assert {p["name"] for p in bob["data"]} == {"Bob B"}


async def test_same_name_allowed_across_users(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Two users may each own a project with the same name (per-owner uniqueness)."""
    a = await multi_user_client.post(
        "/v1/projects", json={"name": "Shared"}, headers=_as_user(ALICE)
    )
    b = await multi_user_client.post(
        "/v1/projects", json={"name": "Shared"}, headers=_as_user(BOB)
    )
    assert a.status_code == 200
    assert b.status_code == 200


async def test_cannot_get_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Bob's project is 404 (not found), never readable, for Alice."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(ALICE))
    assert resp.status_code == 404
    # The owner still sees it.
    assert (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).status_code == 200


async def test_cannot_rename_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Alice cannot rename Bob's project (404), and it stays unchanged."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.patch(
        f"/v1/projects/{created['id']}", json={"name": "Hacked"}, headers=_as_user(ALICE)
    )
    assert resp.status_code == 404
    still = (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).json()
    assert still["name"] == "Bob only"


async def test_cannot_delete_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Alice cannot delete Bob's project (404), and it survives."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.delete(f"/v1/projects/{created['id']}", headers=_as_user(ALICE))
    assert resp.status_code == 404
    assert (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).status_code == 200


# ── Project resources (non-agent workspace tabs) ───────────────────────


async def _make_project(client: httpx.AsyncClient, name: str) -> str:
    """Create a project and return its id."""
    resp = await client.post("/v1/projects", json={"name": name})
    assert resp.status_code == 200
    project_id: str = resp.json()["id"]
    return project_id


async def test_add_resource(project_client: httpx.AsyncClient) -> None:
    """POST attaches a resource and echoes it back."""
    project_id = await _make_project(project_client, "Feature X")
    resp = await project_client.post(
        f"/v1/projects/{project_id}/resources",
        json={
            "type": "link",
            "name": "  Tracking issue  ",
            "uri": "https://github.com/o/r/issues/3",
            "details": {"tab": "github"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "project.resource"
    assert body["project_id"] == project_id
    assert body["type"] == "link"
    assert body["name"] == "Tracking issue"
    assert body["uri"] == "https://github.com/o/r/issues/3"
    assert body["details"] == {"tab": "github"}
    assert body["updated_at"] is None


async def test_add_resource_rejects_unknown_type(project_client: httpx.AsyncClient) -> None:
    """An unknown resource type is a 422 from the request schema."""
    project_id = await _make_project(project_client, "P")
    resp = await project_client.post(
        f"/v1/projects/{project_id}/resources", json={"type": "spaceship", "name": "Bad"}
    )
    assert resp.status_code == 422


async def test_add_resource_unknown_project_404(project_client: httpx.AsyncClient) -> None:
    """Attaching to a project that doesn't exist returns 404."""
    resp = await project_client.post(
        f"/v1/projects/{'0' * 32}/resources", json={"type": "note", "name": "Orphan"}
    )
    assert resp.status_code == 404


async def test_list_resources(project_client: httpx.AsyncClient) -> None:
    """GET lists the project's resources."""
    project_id = await _make_project(project_client, "P")
    for name in ("First", "Second"):
        await project_client.post(
            f"/v1/projects/{project_id}/resources", json={"type": "note", "name": name}
        )
    resp = await project_client.get(f"/v1/projects/{project_id}/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert {r["name"] for r in body["data"]} == {"First", "Second"}


async def test_get_and_update_resource(project_client: httpx.AsyncClient) -> None:
    """A resource can be fetched and patched; type stays put."""
    project_id = await _make_project(project_client, "P")
    created = (
        await project_client.post(
            f"/v1/projects/{project_id}/resources",
            json={"type": "service", "name": "Dev server", "uri": "http://localhost:3000"},
        )
    ).json()

    fetched = await project_client.get(f"/v1/projects/{project_id}/resources/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Dev server"

    patched = await project_client.patch(
        f"/v1/projects/{project_id}/resources/{created['id']}",
        json={"name": "Dev server (vite)", "uri": "http://localhost:5173"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "Dev server (vite)"
    assert body["uri"] == "http://localhost:5173"
    assert body["type"] == "service"
    assert body["updated_at"] is not None


async def test_update_resource_rejects_type_change(project_client: httpx.AsyncClient) -> None:
    """``type`` is not a patchable field."""
    project_id = await _make_project(project_client, "P")
    created = (
        await project_client.post(
            f"/v1/projects/{project_id}/resources", json={"type": "note", "name": "N"}
        )
    ).json()
    resp = await project_client.patch(
        f"/v1/projects/{project_id}/resources/{created['id']}", json={"type": "link"}
    )
    assert resp.status_code == 422


async def test_resource_not_reachable_through_other_project(
    project_client: httpx.AsyncClient,
) -> None:
    """A resource is only addressable under the project that owns it."""
    owning = await _make_project(project_client, "Owning")
    other = await _make_project(project_client, "Other")
    created = (
        await project_client.post(
            f"/v1/projects/{owning}/resources", json={"type": "note", "name": "N"}
        )
    ).json()
    resp = await project_client.get(f"/v1/projects/{other}/resources/{created['id']}")
    assert resp.status_code == 404


async def test_delete_resource(project_client: httpx.AsyncClient) -> None:
    """DELETE detaches the resource; a second delete 404s."""
    project_id = await _make_project(project_client, "P")
    created = (
        await project_client.post(
            f"/v1/projects/{project_id}/resources", json={"type": "note", "name": "Doomed"}
        )
    ).json()
    resp = await project_client.delete(f"/v1/projects/{project_id}/resources/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    again = await project_client.delete(f"/v1/projects/{project_id}/resources/{created['id']}")
    assert again.status_code == 404


async def test_deleting_project_drops_its_resources(project_client: httpx.AsyncClient) -> None:
    """A project's resources go away with the project."""
    project_id = await _make_project(project_client, "Doomed")
    await project_client.post(
        f"/v1/projects/{project_id}/resources", json={"type": "note", "name": "N"}
    )
    await project_client.delete(f"/v1/projects/{project_id}")
    resp = await project_client.get(f"/v1/projects/{project_id}/resources")
    assert resp.status_code == 404


async def test_resources_are_owner_private(multi_user_client: httpx.AsyncClient) -> None:
    """Alice can neither read nor mutate the resources of Bob's project."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resource = (
        await multi_user_client.post(
            f"/v1/projects/{created['id']}/resources",
            json={"type": "link", "name": "Bob's link", "uri": "https://example.com"},
            headers=_as_user(BOB),
        )
    ).json()

    assert (
        await multi_user_client.get(
            f"/v1/projects/{created['id']}/resources", headers=_as_user(ALICE)
        )
    ).status_code == 404
    assert (
        await multi_user_client.patch(
            f"/v1/projects/{created['id']}/resources/{resource['id']}",
            json={"name": "Hacked"},
            headers=_as_user(ALICE),
        )
    ).status_code == 404
    assert (
        await multi_user_client.delete(
            f"/v1/projects/{created['id']}/resources/{resource['id']}",
            headers=_as_user(ALICE),
        )
    ).status_code == 404
    still = (
        await multi_user_client.get(
            f"/v1/projects/{created['id']}/resources/{resource['id']}", headers=_as_user(BOB)
        )
    ).json()
    assert still["name"] == "Bob's link"
