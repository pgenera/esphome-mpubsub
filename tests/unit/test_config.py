"""Validation tests for the YAML schema.

These shell out to the locally installed ``esphome`` binary and assert that
valid configurations are accepted and that each negative case is rejected
with a sensible message. They double as the canonical accept/reject contract
for the schema.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ESPHOME = shutil.which("esphome")


pytestmark = pytest.mark.skipif(ESPHOME is None, reason="esphome binary not found on PATH")


def _esphome_config(yaml_text: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "x.yaml"
    f.write_text(yaml_text)
    return subprocess.run(
        [ESPHOME, "config", str(f)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        env={**os.environ, "ESPHOME_QUICKWIZARD": "1"},
    )


def _wrap(body: str) -> str:
    return (
        "esphome:\n"
        "  name: cfgtest\n"
        "host:\n"
        "logger:\n"
        "external_components:\n"
        "  - source:\n"
        "      type: local\n"
        f"      path: {REPO}/components\n"
        "    components: [multicast_pubsub]\n"
        + body
    )


@pytest.mark.parametrize(
    "scope",
    ["link-local", "site-local", "organization-local"],
)
def test_accepts_all_scopes(tmp_path: Path, scope: str) -> None:
    r = _esphome_config(_wrap(f"multicast_pubsub:\n  scope: {scope}\n"), tmp_path)
    assert "Configuration is valid" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_rejects_invalid_scope(tmp_path: Path) -> None:
    r = _esphome_config(_wrap("multicast_pubsub:\n  scope: planet-local\n"), tmp_path)
    assert r.returncode != 0
    assert "scope" in (r.stdout + r.stderr).lower()


def test_rejects_invalid_port(tmp_path: Path) -> None:
    r = _esphome_config(_wrap("multicast_pubsub:\n  port: 99999\n"), tmp_path)
    assert r.returncode != 0


def test_rejects_invalid_hops(tmp_path: Path) -> None:
    r = _esphome_config(_wrap("multicast_pubsub:\n  hops: 0\n"), tmp_path)
    assert r.returncode != 0


def test_rejects_empty_topic_on_message(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        multicast_pubsub:
          on_message:
            - topic: ""
              then:
                - logger.log: "hi"
        """
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert r.returncode != 0


def test_rejects_oversize_topic_on_message(tmp_path: Path) -> None:
    body = textwrap.dedent(
        f"""\
        multicast_pubsub:
          on_message:
            - topic: "{'x' * 201}"
              then:
                - logger.log: "hi"
        """
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert r.returncode != 0


def test_rejects_oversize_static_payload(tmp_path: Path) -> None:
    # The Python codegen knows MAX_PAYLOAD=1220 and must reject statically-
    # known publish payloads above it at config time, before the device boots.
    big = "x" * 1221
    body = (
        "multicast_pubsub:\n"
        "  id: pubsub\n"
        "sensor:\n"
        "  - platform: template\n"
        "    id: src\n"
        "    lambda: 'return 1.0;'\n"
        "    update_interval: never\n"
        "    on_value:\n"
        "      - multicast_pubsub.publish:\n"
        "          topic: \"test/v\"\n"
        f"          payload: \"{big}\"\n"
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert r.returncode != 0
    combined = (r.stdout + r.stderr).lower()
    assert "maximum publishable size" in combined or "1220" in combined, combined


def test_accepts_max_size_static_payload(tmp_path: Path) -> None:
    # Exactly MAX_PAYLOAD bytes (1220) must still validate.
    payload = "x" * 1220
    body = (
        "multicast_pubsub:\n"
        "  id: pubsub\n"
        "sensor:\n"
        "  - platform: template\n"
        "    id: src\n"
        "    lambda: 'return 1.0;'\n"
        "    update_interval: never\n"
        "    on_value:\n"
        "      - multicast_pubsub.publish:\n"
        "          topic: \"test/v\"\n"
        f"          payload: \"{payload}\"\n"
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert "Configuration is valid" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_rejects_publish_with_both_payload_and_message(tmp_path: Path) -> None:
    body = (
        "multicast_pubsub:\n"
        "  id: pubsub\n"
        "  messages:\n"
        "    - id: m\n"
        "      fields:\n"
        "        - { name: v, type: int32, tag: 1 }\n"
        "api:\n"
        "sensor:\n"
        "  - platform: template\n"
        "    id: src\n"
        "    lambda: 'return 1.0;'\n"
        "    update_interval: never\n"
        "    on_value:\n"
        "      - multicast_pubsub.publish:\n"
        "          topic: \"t\"\n"
        "          payload: \"x\"\n"
        "          message: m\n"
        "          values: { v: 1 }\n"
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert r.returncode != 0
    assert "mutually exclusive" in (r.stdout + r.stderr).lower(), r.stdout + r.stderr


def test_rejects_publish_with_neither_payload_nor_message(tmp_path: Path) -> None:
    body = (
        "multicast_pubsub:\n"
        "  id: pubsub\n"
        "sensor:\n"
        "  - platform: template\n"
        "    id: src\n"
        "    lambda: 'return 1.0;'\n"
        "    update_interval: never\n"
        "    on_value:\n"
        "      - multicast_pubsub.publish:\n"
        "          topic: \"t\"\n"
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert r.returncode != 0
    combined = (r.stdout + r.stderr).lower()
    assert "requires either" in combined or "payload" in combined, combined


def test_rejects_publish_with_message_but_no_values(tmp_path: Path) -> None:
    body = (
        "multicast_pubsub:\n"
        "  id: pubsub\n"
        "  messages:\n"
        "    - id: m\n"
        "      fields:\n"
        "        - { name: v, type: int32, tag: 1 }\n"
        "api:\n"
        "sensor:\n"
        "  - platform: template\n"
        "    id: src\n"
        "    lambda: 'return 1.0;'\n"
        "    update_interval: never\n"
        "    on_value:\n"
        "      - multicast_pubsub.publish:\n"
        "          topic: \"t\"\n"
        "          message: m\n"
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert r.returncode != 0
    assert "values" in (r.stdout + r.stderr).lower()


def test_accepts_publish_typed_in_automation(tmp_path: Path) -> None:
    body = (
        "multicast_pubsub:\n"
        "  id: pubsub\n"
        "  messages:\n"
        "    - id: room_climate\n"
        "      fields:\n"
        "        - { name: temperature, type: float,  tag: 1 }\n"
        "        - { name: room_id,     type: string, tag: 2 }\n"
        "api:\n"
        "sensor:\n"
        "  - platform: template\n"
        "    id: src\n"
        "    lambda: 'return 1.0;'\n"
        "    update_interval: never\n"
        "    on_value:\n"
        "      - multicast_pubsub.publish:\n"
        "          topic: \"home/climate\"\n"
        "          message: room_climate\n"
        "          values:\n"
        "            temperature: !lambda 'return x;'\n"
        "            room_id: \"garage\"\n"
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert "Configuration is valid" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_accepts_publish_action_in_automation(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        multicast_pubsub:
          id: pubsub
        sensor:
          - platform: template
            id: src
            lambda: 'return 1.0;'
            update_interval: never
            on_value:
              - multicast_pubsub.publish:
                  topic: "test/v"
                  payload: !lambda 'return "1";'
        """
    )
    r = _esphome_config(_wrap(body), tmp_path)
    assert "Configuration is valid" in (r.stdout + r.stderr), r.stdout + r.stderr
