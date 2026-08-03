"""Project store — persists first-class, owner-private projects.

A project is a user-defined container that groups sessions and exists
independently of its members (see ``designs/PROJECTS_PRD.md``). This store owns
the ``projects`` table. Session→project membership lives on the conversation's
metadata row (``project_id``) and is managed by the conversation store, not
here. The store also owns ``project_resources`` — the non-agent artifacts
(links, repositories, services, notes) attached to a project — because they
live and die with their project row.

Projects have no ACL of their own (PRD §9): every method is scoped by
``owner_user_id`` so a caller only ever sees and mutates their own projects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import Project, ProjectResource, ProjectResourceType


class ProjectStore(ABC):
    """
    Abstract base for project persistence.

    Manages the lifecycle of projects (CRUD). All reads and writes are scoped
    by ``owner_user_id`` because projects are owner-private.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the project store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        project_id: str,
        name: str,
        owner_user_id: str | None,
        config: dict[str, Any] | None = None,
    ) -> Project:
        """
        Insert a new, empty project.

        :param project_id: Pre-generated unique project id (a UUID string).
        :param name: Human-readable project name. Trimmed, non-empty, unique
            among the owner's projects.
        :param owner_user_id: Owning user, or ``None`` in single-user mode.
        :param config: Optional default session settings (opaque JSON object);
            ``None`` or empty stores no defaults.
        :returns: The newly created :class:`Project`.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the owner already has a
            project with this name.
        """
        ...

    @abstractmethod
    def get(self, project_id: str, *, owner_user_id: str | None) -> Project | None:
        """
        Return an owned project by id, or ``None`` if not found.

        :param project_id: Opaque project identifier.
        :param owner_user_id: The requesting owner; a project owned by someone
            else is treated as not found.
        :returns: The :class:`Project` if found and owned, else ``None``.
        """
        ...

    @abstractmethod
    def list(self, *, owner_user_id: str | None) -> list[Project]:
        """
        List the owner's projects ordered by ``created_at ASC, id ASC``.

        :param owner_user_id: The owner whose projects to return.
        :returns: List of :class:`Project` instances.
        """
        ...

    @abstractmethod
    def update(
        self,
        project_id: str,
        *,
        owner_user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project | None:
        """
        Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``owner_user_id``.

        :param project_id: Opaque project identifier.
        :param owner_user_id: The requesting owner.
        :param name: New name, or ``None`` to leave unchanged. Trimmed,
            non-empty, unique among the owner's projects.
        :param config: New config object to replace the stored one, or ``None``
            to leave it unchanged. An empty dict clears the stored defaults.
        :returns: The updated :class:`Project`, or ``None`` if not found.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the new name collides with
            another of the owner's projects.
        """
        ...

    @abstractmethod
    def delete(self, project_id: str, *, owner_user_id: str | None) -> bool:
        """
        Delete an owned project. Idempotent.

        Deleting a project does not delete its member sessions; unfiling them
        (clearing ``project_id``) is the caller's responsibility. Its resources
        (``project_resources``) belong to the project alone and are deleted
        with it.

        :param project_id: Opaque project identifier.
        :param owner_user_id: The requesting owner.
        :returns: ``True`` if removed; ``False`` if not found / not owned.
        """
        ...

    @abstractmethod
    def add_resource(
        self,
        project_id: str,
        resource_id: str,
        *,
        owner_user_id: str | None,
        type: ProjectResourceType,
        name: str,
        uri: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ProjectResource | None:
        """
        Attach a non-agent resource to an owned project.

        :param project_id: The project to attach to.
        :param resource_id: Pre-generated unique resource id (a UUID string).
        :param owner_user_id: The requesting owner; resources inherit the
            project's owner-private scope.
        :param type: Resource kind, one of
            :data:`~omnigent.entities.PROJECT_RESOURCE_TYPES`.
        :param name: Human-readable label. Trimmed, non-empty.
        :param uri: Location of the resource, or ``None`` when it has none.
        :param details: Optional opaque JSON object; ``None`` or empty stores
            nothing.
        :returns: The created :class:`ProjectResource`, or ``None`` if the
            project does not exist or is not owned by ``owner_user_id``.
        :raises OmnigentError: ``INVALID_INPUT`` for an unknown ``type``.
        """
        ...

    @abstractmethod
    def list_resources(
        self, project_id: str, *, owner_user_id: str | None
    ) -> list[ProjectResource] | None:
        """
        List an owned project's resources ordered by ``created_at ASC, id ASC``.

        :param project_id: The project whose resources to return.
        :param owner_user_id: The requesting owner.
        :returns: The project's resources (possibly empty), or ``None`` if the
            project does not exist or is not owned by ``owner_user_id``.
        """
        ...

    @abstractmethod
    def get_resource(
        self, project_id: str, resource_id: str, *, owner_user_id: str | None
    ) -> ProjectResource | None:
        """
        Return one resource of an owned project, or ``None`` if not found.

        :param project_id: The project the resource must belong to.
        :param resource_id: Opaque resource identifier.
        :param owner_user_id: The requesting owner.
        :returns: The :class:`ProjectResource`, else ``None``.
        """
        ...

    @abstractmethod
    def update_resource(
        self,
        project_id: str,
        resource_id: str,
        *,
        owner_user_id: str | None,
        name: str | None = None,
        uri: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ProjectResource | None:
        """
        Update mutable fields of a resource. ``None`` leaves a field unchanged.

        The resource ``type`` is immutable: a link and a note are different
        things, so callers replace rather than retype.

        :param project_id: The project the resource must belong to.
        :param resource_id: Opaque resource identifier.
        :param owner_user_id: The requesting owner.
        :param name: New label, or ``None`` to leave unchanged.
        :param uri: New location, or ``None`` to leave unchanged. The empty
            string clears it.
        :param details: New details object replacing the stored one, or
            ``None`` to leave unchanged. An empty dict clears it.
        :returns: The updated resource, or ``None`` if not found / not owned.
        """
        ...

    @abstractmethod
    def delete_resource(
        self, project_id: str, resource_id: str, *, owner_user_id: str | None
    ) -> bool:
        """
        Detach a resource from an owned project. Idempotent.

        :param project_id: The project the resource must belong to.
        :param resource_id: Opaque resource identifier.
        :param owner_user_id: The requesting owner.
        :returns: ``True`` if removed; ``False`` if not found / not owned.
        """
        ...
