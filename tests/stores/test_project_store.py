"""Tests for :class:`SqlAlchemyProjectStore`.

Exercises ``create``, ``get``, ``list``, ``update`` and ``delete`` against a
real SQLite database, covering owner scoping and per-owner name uniqueness.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


# projects.id is a Uuid16 column (16 raw bytes) read back as bare 32-char hex.
# ``_uid`` maps a readable seed to a deterministic bare-hex UUID so tests stay
# legible while the store still round-trips real UUIDs.
def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyProjectStore:
    """A fresh :class:`SqlAlchemyProjectStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyProjectStore` instance.
    """
    return SqlAlchemyProjectStore(db_uri)


# ── create / get ──────────────────────────────────────────────────────────


def test_create_returns_project(store: SqlAlchemyProjectStore) -> None:
    """``create`` echoes the fields back and stamps ``created_at``."""
    project = store.create(_uid("p1"), "My Project", "alice@example.com")
    assert project.id == _uid("p1")
    assert project.name == "My Project"
    assert project.owner_user_id == "alice@example.com"
    assert project.created_at > 0
    assert project.updated_at is None


def test_get_returns_created_project(store: SqlAlchemyProjectStore) -> None:
    """``get`` reads back a created project for its owner."""
    store.create(_uid("p1"), "My Project", "alice@example.com")
    got = store.get(_uid("p1"), owner_user_id="alice@example.com")
    assert got is not None
    assert got.name == "My Project"


def test_get_missing_returns_none(store: SqlAlchemyProjectStore) -> None:
    """``get`` returns ``None`` for an unknown id."""
    assert store.get(_uid("nope"), owner_user_id="alice@example.com") is None


def test_get_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A project owned by someone else reads back as not found."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    assert store.get(_uid("p1"), owner_user_id="bob@example.com") is None


# ── list ──────────────────────────────────────────────────────────────────


def test_list_orders_by_created_at_then_id(store: SqlAlchemyProjectStore) -> None:
    """``list`` orders by ``created_at ASC, id ASC``.

    Both rows are created in the same second here, so the ``id`` tiebreaker
    decides the order — assert against that rather than insertion order.
    """
    store.create(_uid("p1"), "First", "alice@example.com")
    store.create(_uid("p2"), "Second", "alice@example.com")
    listed = store.list(owner_user_id="alice@example.com")
    assert {p.name for p in listed} == {"First", "Second"}
    # Whatever the tie order, it is ascending by (created_at, id).
    keys = [(p.created_at, p.id) for p in listed]
    assert keys == sorted(keys)


def test_list_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """``list`` only returns the requesting owner's projects."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    store.create(_uid("p2"), "Bob Project", "bob@example.com")
    alice = store.list(owner_user_id="alice@example.com")
    assert [p.name for p in alice] == ["Alice Project"]


def test_list_empty(store: SqlAlchemyProjectStore) -> None:
    """``list`` returns an empty list when the owner has no projects."""
    assert store.list(owner_user_id="alice@example.com") == []


# ── single-user (None owner) vs multi-user isolation ────────────────────────


def test_none_owner_and_named_owner_are_isolated(store: SqlAlchemyProjectStore) -> None:
    """The single-user ``None`` owner is a distinct scope from any named user.

    A project created in single-user mode (``owner_user_id=None``) must not be
    visible to a named multi-user identity, and vice versa — the same DB can
    hold both without cross-leaking.
    """
    store.create(_uid("solo"), "Solo Project", None)
    store.create(_uid("alice"), "Alice Project", "alice@example.com")

    # Each scope lists only its own.
    assert [p.name for p in store.list(owner_user_id=None)] == ["Solo Project"]
    assert [p.name for p in store.list(owner_user_id="alice@example.com")] == ["Alice Project"]


def test_named_owner_cannot_get_none_owner_project(store: SqlAlchemyProjectStore) -> None:
    """A ``None``-owner project is not found for a named user (and vice versa)."""
    store.create(_uid("solo"), "Solo", None)
    assert store.get(_uid("solo"), owner_user_id="alice@example.com") is None
    assert store.get(_uid("solo"), owner_user_id=None) is not None


def test_named_owner_cannot_mutate_none_owner_project(store: SqlAlchemyProjectStore) -> None:
    """update / delete on a ``None``-owner project are no-ops for a named user."""
    store.create(_uid("solo"), "Solo", None)
    updated = store.update(_uid("solo"), owner_user_id="alice@example.com", name="Hacked")
    assert updated is None
    deleted = store.delete(_uid("solo"), owner_user_id="alice@example.com")
    assert deleted is False
    # Untouched for the real (None) owner.
    assert store.get(_uid("solo"), owner_user_id=None).name == "Solo"


# ── name uniqueness ────────────────────────────────────────────────────────


def test_create_rejects_duplicate_name_per_owner(store: SqlAlchemyProjectStore) -> None:
    """Two projects with the same name for one owner are rejected."""
    store.create(_uid("p1"), "Dup", "alice@example.com")
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Dup", "alice@example.com")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_same_name_allowed_across_owners(store: SqlAlchemyProjectStore) -> None:
    """Two different owners may each have a project with the same name."""
    a = store.create(_uid("p1"), "Shared Name", "alice@example.com")
    b = store.create(_uid("p2"), "Shared Name", "bob@example.com")
    assert a.name == b.name == "Shared Name"


def test_duplicate_name_rejected_for_null_owner(store: SqlAlchemyProjectStore) -> None:
    """Single-user mode (NULL owner) still enforces name uniqueness.

    The DB UNIQUE index can't do this (SQL treats NULLs as distinct), so the
    store's ``_name_taken`` check is the sole guard for NULL owners.
    """
    store.create(_uid("p1"), "Solo", None)
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Solo", None)
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_duplicate_name_rejected_at_db_layer_for_named_owner(
    store: SqlAlchemyProjectStore,
) -> None:
    """The UNIQUE index enforces per-owner uniqueness even if the store's
    ``_name_taken`` pre-check is bypassed — the DB is the race backstop.

    Monkeypatching ``_name_taken`` to always-miss simulates two concurrent
    creates both passing the check; the second must still fail via the index's
    ``IntegrityError``, mapped to ``ALREADY_EXISTS``.
    """
    store.create(_uid("p1"), "Dup", "alice@example.com")
    store._name_taken = lambda *a, **k: False  # type: ignore[method-assign]
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Dup", "alice@example.com")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_non_name_integrity_error_is_not_masked(store: SqlAlchemyProjectStore) -> None:
    """An integrity failure that isn't the name index re-raises untranslated.

    Reusing an existing id hits the primary-key constraint, not
    ``ix_projects_name``; that must surface as ``IntegrityError`` rather than a
    misleading ``ALREADY_EXISTS`` name collision.
    """
    store.create(_uid("p1"), "Original", "alice@example.com")
    store._name_taken = lambda *a, **k: False  # type: ignore[method-assign]
    with pytest.raises(IntegrityError):
        store.create(_uid("p1"), "Different name", "alice@example.com")


# ── config (default session settings) ──────────────────────────────────────


def test_create_defaults_to_empty_config(store: SqlAlchemyProjectStore) -> None:
    """A project created without config reads back an empty dict (SQL NULL)."""
    project = store.create(_uid("p1"), "No Config", "alice@example.com")
    assert project.config == {}
    assert store.get(_uid("p1"), owner_user_id="alice@example.com").config == {}


def test_create_persists_config(store: SqlAlchemyProjectStore) -> None:
    """A config passed to ``create`` round-trips through ``get``/``list``."""
    cfg = {"host_id": "host_abc", "workspace": "/work/repo", "model": "claude-opus-4-8"}
    store.create(_uid("p1"), "Configured", "alice@example.com", cfg)
    assert store.get(_uid("p1"), owner_user_id="alice@example.com").config == cfg
    assert store.list(owner_user_id="alice@example.com")[0].config == cfg


def test_update_replaces_config_and_stamps_updated_at(store: SqlAlchemyProjectStore) -> None:
    """Passing a new ``config`` replaces the stored one and stamps updated_at."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "old"})
    updated = store.update(
        _uid("p1"), owner_user_id="alice@example.com", config={"host_id": "new", "model": "m"}
    )
    assert updated is not None
    assert updated.config == {"host_id": "new", "model": "m"}
    assert updated.updated_at is not None


def test_update_config_none_leaves_it_unchanged(store: SqlAlchemyProjectStore) -> None:
    """``config=None`` (the default) leaves the stored config untouched."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "keep"})
    # Rename only — config omitted — must not wipe the stored defaults.
    updated = store.update(_uid("p1"), owner_user_id="alice@example.com", name="Renamed")
    assert updated is not None
    assert updated.config == {"host_id": "keep"}


def test_update_empty_config_clears_defaults(store: SqlAlchemyProjectStore) -> None:
    """An explicit ``config={}`` clears the stored defaults (distinct from None)."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "drop"})
    updated = store.update(_uid("p1"), owner_user_id="alice@example.com", config={})
    assert updated is not None
    assert updated.config == {}
    assert updated.updated_at is not None


def test_update_same_config_is_noop(store: SqlAlchemyProjectStore) -> None:
    """Re-setting the identical config changes nothing, leaving updated_at None."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "x"})
    updated = store.update(_uid("p1"), owner_user_id="alice@example.com", config={"host_id": "x"})
    assert updated is not None
    assert updated.updated_at is None


def test_create_rejects_oversized_config(store: SqlAlchemyProjectStore) -> None:
    """A config whose serialized form exceeds the cap is rejected on create.

    The value is persisted verbatim and reflected back on every read, so an
    unbounded blob is capped as defense-in-depth (INVALID_INPUT → HTTP 400).
    """
    huge = {"blob": "x" * (64 * 1024 + 1)}
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p1"), "Big", "alice@example.com", huge)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_update_rejects_oversized_config(store: SqlAlchemyProjectStore) -> None:
    """An oversized config is rejected on update, leaving the row unchanged."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "keep"})
    huge = {"blob": "x" * (64 * 1024 + 1)}
    with pytest.raises(OmnigentError) as exc:
        store.update(_uid("p1"), owner_user_id="alice@example.com", config=huge)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    # The prior config is untouched (the encode guard fires before any write).
    assert store.get(_uid("p1"), owner_user_id="alice@example.com").config == {"host_id": "keep"}


def test_decode_coerces_non_object_blob_to_empty(store: SqlAlchemyProjectStore) -> None:
    """A stored non-object blob (manual edit / future writer) reads back as {}.

    The encode path only ever writes JSON objects, but ``_decode_config`` must
    stay defensive so callers can always treat config as a mapping.
    """
    from sqlalchemy import text as sa_text

    store.create(_uid("p1"), "P", "alice@example.com")
    # Bypass the store to plant a scalar JSON blob directly.
    with store._engine.begin() as conn:
        conn.execute(
            sa_text("UPDATE projects SET config = :c WHERE id = :i"),
            {"c": '"just a string"', "i": _uid("p1")},
        )
    got = store.get(_uid("p1"), owner_user_id="alice@example.com")
    assert got is not None
    assert got.config == {}


# ── update ─────────────────────────────────────────────────────────────────


def test_update_renames_and_stamps_updated_at(store: SqlAlchemyProjectStore) -> None:
    """Renaming changes ``name`` and sets ``updated_at``."""
    store.create(_uid("p1"), "Old", "alice@example.com")
    updated = store.update(_uid("p1"), owner_user_id="alice@example.com", name="New")
    assert updated is not None
    assert updated.name == "New"
    assert updated.updated_at is not None


def test_update_noop_leaves_updated_at_none(store: SqlAlchemyProjectStore) -> None:
    """An update that changes nothing leaves ``updated_at`` untouched."""
    store.create(_uid("p1"), "Same", "alice@example.com")
    updated = store.update(_uid("p1"), owner_user_id="alice@example.com", name="Same")
    assert updated is not None
    assert updated.updated_at is None


def test_update_missing_returns_none(store: SqlAlchemyProjectStore) -> None:
    """Updating an unknown project returns ``None``."""
    updated = store.update(_uid("nope"), owner_user_id="alice@example.com", name="X")
    assert updated is None


def test_update_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot rename another user's project."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    updated = store.update(_uid("p1"), owner_user_id="bob@example.com", name="Hacked")
    assert updated is None
    # Unchanged for the real owner.
    assert store.get(_uid("p1"), owner_user_id="alice@example.com").name == "Alice Project"


def test_update_rejects_duplicate_name(store: SqlAlchemyProjectStore) -> None:
    """Renaming onto another of the owner's project names is rejected."""
    store.create(_uid("p1"), "First", "alice@example.com")
    store.create(_uid("p2"), "Second", "alice@example.com")
    with pytest.raises(OmnigentError) as exc:
        store.update(_uid("p2"), owner_user_id="alice@example.com", name="First")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


# ── delete ─────────────────────────────────────────────────────────────────


def test_delete_removes_project(store: SqlAlchemyProjectStore) -> None:
    """``delete`` removes the project and is idempotent."""
    store.create(_uid("p1"), "Doomed", "alice@example.com")
    deleted = store.delete(_uid("p1"), owner_user_id="alice@example.com")
    assert deleted is True
    assert store.get(_uid("p1"), owner_user_id="alice@example.com") is None
    deleted_again = store.delete(_uid("p1"), owner_user_id="alice@example.com")
    assert deleted_again is False


def test_delete_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot delete another user's project."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    deleted = store.delete(_uid("p1"), owner_user_id="bob@example.com")
    assert deleted is False
    assert store.get(_uid("p1"), owner_user_id="alice@example.com") is not None


# ── resources ──────────────────────────────────────────────────────────────

ALICE = "alice@example.com"
BOB = "bob@example.com"


def test_add_resource_returns_resource(store: SqlAlchemyProjectStore) -> None:
    """``add_resource`` echoes the fields back and stamps ``created_at``."""
    store.create(_uid("p1"), "P", ALICE)
    resource = store.add_resource(
        _uid("p1"),
        _uid("r1"),
        owner_user_id=ALICE,
        type="link",
        name="Design doc",
        uri="https://example.com/doc",
        details={"tab": "docs"},
    )
    assert resource is not None
    assert resource.project_id == _uid("p1")
    assert resource.type == "link"
    assert resource.name == "Design doc"
    assert resource.uri == "https://example.com/doc"
    assert resource.details == {"tab": "docs"}
    assert resource.created_at > 0
    assert resource.updated_at is None


def test_add_resource_missing_project_returns_none(store: SqlAlchemyProjectStore) -> None:
    """Attaching to an unknown project returns ``None``."""
    assert (
        store.add_resource(
            _uid("nope"), _uid("r1"), owner_user_id=ALICE, type="note", name="Orphan"
        )
        is None
    )


def test_add_resource_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot attach resources to another user's project."""
    store.create(_uid("p1"), "P", ALICE)
    assert (
        store.add_resource(_uid("p1"), _uid("r1"), owner_user_id=BOB, type="note", name="Nope")
        is None
    )


def test_add_resource_rejects_unknown_type(store: SqlAlchemyProjectStore) -> None:
    """An unknown resource type is rejected."""
    store.create(_uid("p1"), "P", ALICE)
    with pytest.raises(OmnigentError) as exc:
        store.add_resource(
            _uid("p1"),
            _uid("r1"),
            owner_user_id=ALICE,
            type="spaceship",  # type: ignore[arg-type]
            name="Bad",
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_list_resources_scoped_to_project(store: SqlAlchemyProjectStore) -> None:
    """A project's resources exclude other projects' resources."""
    store.create(_uid("p1"), "P1", ALICE)
    store.create(_uid("p2"), "P2", ALICE)
    store.add_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, type="link", name="A")
    store.add_resource(_uid("p1"), _uid("r2"), owner_user_id=ALICE, type="note", name="B")
    store.add_resource(_uid("p2"), _uid("r3"), owner_user_id=ALICE, type="note", name="Other")

    resources = store.list_resources(_uid("p1"), owner_user_id=ALICE)
    assert resources is not None
    assert {r.name for r in resources} == {"A", "B"}
    # Stable server order: created_at then id.
    assert [(r.created_at, r.id) for r in resources] == sorted(
        (r.created_at, r.id) for r in resources
    )


def test_list_resources_missing_project_returns_none(store: SqlAlchemyProjectStore) -> None:
    """Listing an unknown (or unowned) project's resources returns ``None``."""
    store.create(_uid("p1"), "P", ALICE)
    assert store.list_resources(_uid("nope"), owner_user_id=ALICE) is None
    assert store.list_resources(_uid("p1"), owner_user_id=BOB) is None


def test_get_resource_requires_matching_project(store: SqlAlchemyProjectStore) -> None:
    """A resource is only reachable through the project that owns it."""
    store.create(_uid("p1"), "P1", ALICE)
    store.create(_uid("p2"), "P2", ALICE)
    store.add_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, type="note", name="A")
    assert store.get_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE) is not None
    assert store.get_resource(_uid("p2"), _uid("r1"), owner_user_id=ALICE) is None
    assert store.get_resource(_uid("p1"), _uid("r1"), owner_user_id=BOB) is None


def test_update_resource_changes_fields(store: SqlAlchemyProjectStore) -> None:
    """``update_resource`` writes the provided fields and stamps ``updated_at``."""
    store.create(_uid("p1"), "P", ALICE)
    store.add_resource(
        _uid("p1"), _uid("r1"), owner_user_id=ALICE, type="service", name="API", uri="http://old"
    )
    updated = store.update_resource(
        _uid("p1"), _uid("r1"), owner_user_id=ALICE, name="API v2", uri="http://new"
    )
    assert updated is not None
    assert updated.name == "API v2"
    assert updated.uri == "http://new"
    assert updated.updated_at is not None


def test_update_resource_empty_uri_clears_it(store: SqlAlchemyProjectStore) -> None:
    """An empty ``uri`` clears the stored location; ``None`` leaves it alone."""
    store.create(_uid("p1"), "P", ALICE)
    store.add_resource(
        _uid("p1"), _uid("r1"), owner_user_id=ALICE, type="link", name="L", uri="http://keep"
    )
    unchanged = store.update_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, name="L2")
    assert unchanged is not None
    assert unchanged.uri == "http://keep"
    cleared = store.update_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, uri="")
    assert cleared is not None
    assert cleared.uri is None


def test_update_resource_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot update another user's resource."""
    store.create(_uid("p1"), "P", ALICE)
    store.add_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, type="note", name="Mine")
    assert store.update_resource(_uid("p1"), _uid("r1"), owner_user_id=BOB, name="Yours") is None


def test_delete_resource_removes_it(store: SqlAlchemyProjectStore) -> None:
    """``delete_resource`` removes the row and is idempotent."""
    store.create(_uid("p1"), "P", ALICE)
    store.add_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, type="note", name="Doomed")
    assert store.delete_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE) is True
    assert store.get_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE) is None
    assert store.delete_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE) is False


def test_delete_project_deletes_its_resources(store: SqlAlchemyProjectStore) -> None:
    """Deleting a project takes its resources with it, leaving others alone."""
    store.create(_uid("p1"), "P1", ALICE)
    store.create(_uid("p2"), "P2", ALICE)
    store.add_resource(_uid("p1"), _uid("r1"), owner_user_id=ALICE, type="note", name="Gone")
    store.add_resource(_uid("p2"), _uid("r2"), owner_user_id=ALICE, type="note", name="Kept")

    store.delete(_uid("p1"), owner_user_id=ALICE)
    store.create(_uid("p3"), "P1 again", ALICE)
    assert store.list_resources(_uid("p3"), owner_user_id=ALICE) == []
    remaining = store.list_resources(_uid("p2"), owner_user_id=ALICE)
    assert remaining is not None
    assert [r.name for r in remaining] == ["Kept"]
