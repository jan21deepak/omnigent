"""Tests for pack package-root import support."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent.spec.pack_imports import (
    ensure_pack_root_importable,
    find_pack_root,
    unimportable_function_policy_paths,
)
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
)


def _make_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "packroot_agents" / "mypack"
    (pack / "policies").mkdir(parents=True)
    (tmp_path / "packroot_agents" / "__init__.py").write_text("")
    (pack / "__init__.py").write_text("")
    (pack / "policies" / "__init__.py").write_text("")
    (pack / "policies" / "custom_policy.py").write_text(
        "def my_factory():\n    return lambda event, config: {'result': 'ALLOW'}\n",
    )
    (pack / "config.yaml").write_text("name: mypack\n")
    return pack


def _spec(path: str) -> AgentSpec:
    return AgentSpec(
        spec_version="1",
        name="mypack",
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="my_policy",
                    on=None,
                    function=FunctionRef(path=path),
                ),
            ],
        ),
    )


def test_find_pack_root_walks_up_package_parents(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path)
    assert find_pack_root(pack) == tmp_path.resolve()


def test_find_pack_root_none_for_plain_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert find_pack_root(plain) is None


def test_ensure_pack_root_importable_allows_dotted_import(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    pack = _make_pack(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    spec = _spec("packroot_agents.mypack.policies.custom_policy.my_factory")

    assert unimportable_function_policy_paths(spec)
    assert ensure_pack_root_importable(pack) == tmp_path.resolve()
    assert unimportable_function_policy_paths(spec) == []


def test_unimportable_paths_report_reason() -> None:
    failures = unimportable_function_policy_paths(_spec("nope_pack.policies.thing"))
    assert len(failures) == 1
    assert failures[0][0] == "nope_pack.policies.thing"
    assert "ModuleNotFoundError" in failures[0][1]


def test_sub_agent_policy_paths_are_checked() -> None:
    parent = AgentSpec(
        spec_version="1",
        name="parent",
        sub_agents=[_spec("nope_pack.policies.thing")],
    )
    assert unimportable_function_policy_paths(parent)
