"""REST API routes for projects (``/v1/projects``).

Projects are first-class, owner-private containers that group sessions (see
``designs/PROJECTS_PRD.md``). These endpoints let the web UI create empty
projects, list them, rename them, and delete them. Session membership
(filing a session into a project) is managed on the sessions API via the
conversation store's ``project_id``.

The nested ``/projects/{id}/resources`` endpoints attach the non-agent half of
a workspace — links, repositories, local services and notes — so everything
behind one objective lives in one place instead of scattered browser tabs.

Because projects are owner-private and carry no ACL of their own, every handler
scopes to the requesting user: a caller only ever sees and mutates their own
projects. In single-user mode (no auth provider) the owner is ``None`` and all
projects share that scope.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Request

from omnigent.entities import Project, ProjectResource
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    CreateProjectRequest,
    CreateProjectResourceRequest,
    UpdateProjectRequest,
    UpdateProjectResourceRequest,
)
from omnigent.stores.project_store import ProjectStore


def _to_response(project: Project) -> dict[str, Any]:
    """Convert a :class:`Project` entity to a ``ProjectObject`` response dict.

    :param project: The entity to convert.
    :returns: Dict matching the :class:`ProjectObject` shape.
    """
    return {
        "id": project.id,
        "object": "project",
        "name": project.name,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "config": project.config,
    }


def _resource_to_response(resource: ProjectResource) -> dict[str, Any]:
    """Convert a :class:`ProjectResource` to a ``ProjectResourceObject`` dict.

    :param resource: The entity to convert.
    :returns: Dict matching the :class:`ProjectResourceObject` shape.
    """
    return {
        "id": resource.id,
        "object": "project.resource",
        "project_id": resource.project_id,
        "type": resource.type,
        "name": resource.name,
        "uri": resource.uri,
        "details": resource.details,
        "created_at": resource.created_at,
        "updated_at": resource.updated_at,
    }


def create_projects_router(
    project_store: ProjectStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the projects router (``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.post("/projects")
    async def create_project(
        request: Request,
        body: CreateProjectRequest,
    ) -> dict[str, Any]:
        """Create a new, empty project owned by the caller.

        :param request: The incoming request, used to identify the user.
        :param body: Project payload (name).
        :returns: The created project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 409
            if the caller already has a project with this name.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(
            project_store.create,
            uuid.uuid4().hex,
            body.name,
            user_id,
            body.config,
        )
        return _to_response(project)

    @router.get("/projects")
    async def list_projects(request: Request) -> dict[str, Any]:
        """List the caller's projects.

        :param request: The incoming request, used to identify the user.
        :returns: ``{"object": "list", "data": [...]}``.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode.
        """
        user_id = require_user(request, auth_provider)
        projects = await asyncio.to_thread(project_store.list, owner_user_id=user_id)
        return {"object": "list", "data": [_to_response(p) for p in projects]}

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str) -> dict[str, Any]:
        """Return one of the caller's projects.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to fetch.
        :returns: The project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, owner_user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return _to_response(project)

    @router.patch("/projects/{project_id}")
    async def update_project(
        request: Request,
        project_id: str,
        body: UpdateProjectRequest,
    ) -> dict[str, Any]:
        """Update one of the caller's projects (e.g. rename).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to update.
        :param body: Fields to change; ``None`` fields are left unchanged.
        :returns: The updated project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned, 409 if the new name collides with another of the caller's
            projects.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(
            project_store.update,
            project_id,
            owner_user_id=user_id,
            name=body.name,
            config=body.config,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return _to_response(project)

    @router.delete("/projects/{project_id}")
    async def delete_project(request: Request, project_id: str) -> dict[str, Any]:
        """Delete one of the caller's projects.

        Member sessions are not deleted; they are left for the caller to
        unfile (clearing their ``project_id``).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to delete.
        :returns: ``{"id": ..., "object": "project.deleted", "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        deleted = await asyncio.to_thread(project_store.delete, project_id, owner_user_id=user_id)
        if not deleted:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return {"id": project_id, "object": "project.deleted", "deleted": True}

    @router.post("/projects/{project_id}/resources")
    async def add_project_resource(
        request: Request,
        project_id: str,
        body: CreateProjectResourceRequest,
    ) -> dict[str, Any]:
        """Attach a non-agent resource (link, repo, service, note) to a project.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to attach the resource to.
        :param body: Resource payload (type, name, uri, details).
        :returns: The created resource as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project is
            not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        resource = await asyncio.to_thread(
            project_store.add_resource,
            project_id,
            uuid.uuid4().hex,
            owner_user_id=user_id,
            type=body.type,
            name=body.name,
            uri=body.uri,
            details=body.details,
        )
        if resource is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return _resource_to_response(resource)

    @router.get("/projects/{project_id}/resources")
    async def list_project_resources(request: Request, project_id: str) -> dict[str, Any]:
        """List a project's resources, oldest first.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project whose resources to list.
        :returns: ``{"object": "list", "data": [...]}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project is
            not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        resources = await asyncio.to_thread(
            project_store.list_resources, project_id, owner_user_id=user_id
        )
        if resources is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return {"object": "list", "data": [_resource_to_response(r) for r in resources]}

    @router.get("/projects/{project_id}/resources/{resource_id}")
    async def get_project_resource(
        request: Request, project_id: str, resource_id: str
    ) -> dict[str, Any]:
        """Return one resource of the caller's project.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the resource belongs to.
        :param resource_id: The resource to fetch.
        :returns: The resource as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if the resource or
            its project is not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        resource = await asyncio.to_thread(
            project_store.get_resource, project_id, resource_id, owner_user_id=user_id
        )
        if resource is None:
            raise OmnigentError("Project resource not found", code=ErrorCode.NOT_FOUND)
        return _resource_to_response(resource)

    @router.patch("/projects/{project_id}/resources/{resource_id}")
    async def update_project_resource(
        request: Request,
        project_id: str,
        resource_id: str,
        body: UpdateProjectResourceRequest,
    ) -> dict[str, Any]:
        """Update a resource's label, location, or details.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the resource belongs to.
        :param resource_id: The resource to update.
        :param body: Fields to change; ``None`` fields are left unchanged.
        :returns: The updated resource as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if the resource or
            its project is not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        resource = await asyncio.to_thread(
            project_store.update_resource,
            project_id,
            resource_id,
            owner_user_id=user_id,
            name=body.name,
            uri=body.uri,
            details=body.details,
        )
        if resource is None:
            raise OmnigentError("Project resource not found", code=ErrorCode.NOT_FOUND)
        return _resource_to_response(resource)

    @router.delete("/projects/{project_id}/resources/{resource_id}")
    async def delete_project_resource(
        request: Request, project_id: str, resource_id: str
    ) -> dict[str, Any]:
        """Detach a resource from the caller's project.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the resource belongs to.
        :param resource_id: The resource to remove.
        :returns: ``{"id": ..., "object": "project.resource.deleted",
            "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the resource or
            its project is not found / not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        deleted = await asyncio.to_thread(
            project_store.delete_resource, project_id, resource_id, owner_user_id=user_id
        )
        if not deleted:
            raise OmnigentError("Project resource not found", code=ErrorCode.NOT_FOUND)
        return {"id": resource_id, "object": "project.resource.deleted", "deleted": True}

    return router
