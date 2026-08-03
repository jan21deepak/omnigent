"""Project resource entity — persisted in the ``project_resources`` table.

A :class:`ProjectResource` is a non-agent artifact attached to a project: a
link (GitHub PR, doc, design file), a repository, a local service, or a free
text note. Agent sessions are already attached to a project through the
conversation's ``project_id``; resources cover everything else that belongs to
the same piece of work, so a project is a whole workspace rather than a list of
sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

ProjectResourceType = Literal["link", "repository", "document", "service", "note"]

PROJECT_RESOURCE_TYPES: frozenset[str] = frozenset(get_args(ProjectResourceType))


@dataclass
class ProjectResource:
    """
    A non-agent resource attached to a project.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param project_id: Owning project's id. Resources are never shared between
        projects and are deleted with their project.
    :param type: Resource kind, one of :data:`PROJECT_RESOURCE_TYPES`.
    :param name: Human-readable label shown in the workspace UI.
    :param uri: Location of the resource — an URL for links, a path or clone
        URL for repositories, ``http://localhost:3000`` for services — or
        ``None`` for resources that carry no location (notes).
    :param details: Opaque client-owned JSON object for anything the type needs
        (note body, branch, port, …), or an empty dict when none are stored.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    id: str
    project_id: str
    type: ProjectResourceType
    name: str
    uri: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    updated_at: int | None = None
