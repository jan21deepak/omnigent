"""
Import-path support for agent packs that ship their own policy code.

A pack whose ``config.yaml`` references a function policy by dotted
path (``agents.mypack.policies.custom.factory``) can only be resolved
when the package root that dotted path is relative to is on
``sys.path``. Policy evaluation runs in the server process, whose
``sys.path`` otherwise only contains the pack root by accident (when
the server was launched from it), so registration makes the root
importable explicitly and reports paths that still cannot be imported.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from omnigent.spec.types import AgentSpec, FunctionPolicySpec

__all__ = [
    "ensure_pack_root_importable",
    "find_pack_root",
    "unimportable_function_policy_paths",
]


def find_pack_root(source: Path) -> Path | None:
    """
    Return the package root a pack's dotted paths resolve against.

    Walks up from the pack's config directory while each directory is
    a Python package (has ``__init__.py``); the first non-package
    ancestor is the directory that must be importable.

    :param source: The ``--agent`` source: a pack directory or a
        standalone YAML file.
    :returns: The package root, or ``None`` when the pack directory
        is not itself part of a Python package.
    """
    config_dir = source if source.is_dir() else source.parent
    config_dir = config_dir.resolve()
    if not (config_dir / "__init__.py").exists():
        return None
    root = config_dir
    while (root / "__init__.py").exists():
        root = root.parent
    return root


def ensure_pack_root_importable(source: Path) -> Path | None:
    """
    Put the pack's package root on ``sys.path`` if it has one.

    :param source: The ``--agent`` source (directory or YAML file).
    :returns: The root that was made importable, or ``None`` when the
        pack has no package root.
    """
    root = find_pack_root(source)
    if root is None:
        return None
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
        importlib.invalidate_caches()
    return root


def _function_policy_paths(spec: AgentSpec) -> list[str]:
    """
    Collect every function-policy dotted path in *spec*, recursively.

    :param spec: A loaded agent spec.
    :returns: Dotted paths declared by the agent and its sub-agents.
    """
    paths: list[str] = []
    guardrails = spec.guardrails
    if guardrails is not None and guardrails.policies:
        paths.extend(
            policy.function.path
            for policy in guardrails.policies
            if isinstance(policy, FunctionPolicySpec) and policy.function is not None
        )
    for sub_agent in spec.sub_agents or []:
        paths.extend(_function_policy_paths(sub_agent))
    return paths


def unimportable_function_policy_paths(spec: AgentSpec) -> list[tuple[str, str]]:
    """
    Report function-policy paths that cannot be imported.

    Only the module half of each dotted path is imported — the
    attribute lookup is left to policy build time, which reports a
    precise ``AttributeError``.

    :param spec: A loaded agent spec.
    :returns: ``(dotted_path, reason)`` pairs, empty when all resolve.
    """
    failures: list[tuple[str, str]] = []
    for path in _function_policy_paths(spec):
        if "." not in path:
            failures.append(
                (path, "not a dotted module.attribute reference"),
            )
            continue
        module_path = path.rsplit(".", 1)[0]
        try:
            importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - report, don't crash startup
            failures.append((path, f"{type(exc).__name__}: {exc}"))
    return failures
