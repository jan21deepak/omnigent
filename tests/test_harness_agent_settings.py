"""Tests for agent-declared harness settings."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from omnigent.claude_native_bridge import augment_claude_args, build_hook_settings
from omnigent.harness_agent_settings import (
    harness_settings_from_spec,
    merge_claude_settings,
    serialize_harness_settings,
)
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, ExecutorSpec


def test_parse_preserves_harness_settings_structure(tmp_path: Path) -> None:
    bundle = tmp_path / "agent"
    bundle.mkdir()
    (bundle / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "settings-agent",
                "executor": {
                    "type": "omnigent",
                    "config": {
                        "harness": "claude-native",
                        "harness_settings": {
                            "permissions": {"defaultMode": "acceptEdits"},
                            "env": {"AGENT_FLAG": "1"},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    spec = parse(bundle)

    assert spec.executor.config["harness_settings"] == {
        "permissions": {"defaultMode": "acceptEdits"},
        "env": {"AGENT_FLAG": "1"},
    }


def test_harness_settings_from_spec_returns_none_when_absent() -> None:
    spec = AgentSpec(spec_version=1, name="bare", executor=ExecutorSpec())

    assert harness_settings_from_spec(spec) is None


def test_merge_claude_settings_preserves_framework_hooks() -> None:
    agent = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "agent-hook"}]}]},
        "permissions": {"defaultMode": "acceptEdits"},
        "env": {"AGENT_FLAG": "1"},
    }
    framework = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "framework-hook"}]}]},
        "statusLine": {"type": "command", "command": "framework-status"},
        "permissions": {"defaultMode": "auto"},
    }

    merged = merge_claude_settings(agent, framework)

    assert merged["hooks"] == framework["hooks"]
    assert merged["statusLine"] == framework["statusLine"]
    assert merged["permissions"] == {"defaultMode": "auto"}
    assert merged["env"] == {"AGENT_FLAG": "1"}


def test_build_hook_settings_merges_agent_overlay(tmp_path: Path) -> None:
    settings = build_hook_settings(
        tmp_path,
        agent_settings={
            "permissions": {"defaultMode": "acceptEdits"},
            "env": {"AGENT_FLAG": "1"},
        },
        launch_permission_mode="auto",
    )

    assert settings["permissions"] == {"defaultMode": "auto"}
    assert settings["env"] == {"AGENT_FLAG": "1"}
    assert "hooks" in settings
    assert "statusLine" in settings


def test_augment_claude_args_threads_agent_settings(tmp_path: Path) -> None:
    args = augment_claude_args(
        ("--permission-mode", "auto"),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
        agent_settings={
            "permissions": {"defaultMode": "acceptEdits"},
            "env": {"AGENT_FLAG": "1"},
        },
    )

    settings = json.loads(args[args.index("--settings") + 1])
    assert settings["permissions"] == {"defaultMode": "auto"}
    assert settings["env"] == {"AGENT_FLAG": "1"}
    assert "omnigent.claude_native_hook" in settings["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_serialize_harness_settings_is_compact_json() -> None:
    payload = serialize_harness_settings({"permissions": {"defaultMode": "plan"}})

    assert payload == '{"permissions":{"defaultMode":"plan"}}'
