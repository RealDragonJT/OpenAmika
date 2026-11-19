"""openamika-iot — a small first-party MCP server wrapping mosquitto + zigbee2mqtt.

Run (stdio, what OpenAmika's tools.json spawns once the IoT safety statement was accepted on the Tools page):
    python -m openamika_iot
Run as a network server (when the Zigbee dongle lives on another machine):
    python -m openamika_iot --http --host 0.0.0.0 --port 8010 --accept-iot-disclaimer 2026-11-03

It refuses to start until OpenAmika's IoT safety statement (docs/IOT_DISCLAIMER.md) has been accepted: the brain
passes the accepted version in OPENAMIKA_IOT_DISCLAIMER_ACCEPTED, by hand you pass --accept-iot-disclaimer
(see disclaimer.py).

Requires: pip install mcp paho-mqtt     (see pyproject.toml)
"""
from __future__ import annotations

import argparse
import json
import sys

from .disclaimer import IOT_DISCLAIMER_VERSION, refusal


def _fastmcp():
    """The MCP server class, imported only after the safety check (a refusal needs no packages)."""
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:  # newer SDK naming
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            print("The 'mcp' package is required: pip install mcp", file=sys.stderr)
            raise SystemExit(1)
    return FastMCP


def _json(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1)


def build_server(host: str = "127.0.0.1", port: int = 8010):
    FastMCP = _fastmcp()
    from .bridge import BRIDGE, DeviceNotFound
    try:
        mcp = FastMCP("openamika-iot", host=host, port=port)
    except TypeError:
        mcp = FastMCP("openamika-iot")

    @mcp.tool()
    def health() -> str:
        """Check the smart-home connection (MQTT broker + zigbee2mqtt). Self-heals a
        stale connection before reporting. Call this first if a device didn't respond,
        then retry the command once."""
        doc = BRIDGE.health()
        # the brain's health light reads {ok, reason, detail} (same shape as the agent bridges)
        from .bridge import MQTT_BROKER, MQTT_PORT
        doc["ok"] = doc.get("status") == "ok"
        if doc["ok"]:
            doc["reason"], doc["detail"] = "ok", f"MQTT + zigbee2mqtt online · {doc.get('devices_tracked', 0)} device(s)"
        elif not doc.get("mqtt_connected"):
            doc["reason"], doc["detail"] = "unreachable", f"no MQTT broker at {MQTT_BROKER}:{MQTT_PORT} (is mosquitto running?)"
        elif not doc.get("zigbee2mqtt_online"):
            doc["reason"], doc["detail"] = "degraded", "MQTT ok but zigbee2mqtt is offline (not started, or the dongle is not answering)"
        else:
            doc["reason"], doc["detail"] = "degraded", f"no MQTT traffic for {doc.get('mqtt_last_message_ago_s')}s"
        return _json(doc)

    @mcp.tool()
    def list_devices() -> str:
        """List every smart-home device with its nickname, on/off state, availability
        and power metrics. Address devices by nickname or friendly name."""
        return _json(BRIDGE.list_devices())

    @mcp.tool()
    def get_state(device: str) -> str:
        """Read one device's current state and power metrics. `device` is its nickname, its name, or
        plain words describing it ("the lamp in the corner") — matched against nicknames and notes."""
        return _json(BRIDGE.get_device(device))

    @mcp.tool()
    def set_state(device: str, state: str) -> str:
        """Switch a device: state = 'on', 'off' or 'toggle'. `device` is its nickname, its name, or
        plain words describing it ("the lamp in the corner") — matched against nicknames and notes.
        Waits up to 3 s for the device's own confirmation. The reply's `feedback` is one fixed
        sentence (DONE / NO CHANGE / NOT DONE …) saying what really happened."""
        try:
            return _json(BRIDGE.set_state(device, state))
        except ValueError as error:
            return _json({"ok": False, "error": str(error), "feedback": f"NOT DONE — {error}"})

    @mcp.tool()
    def set_nickname(device: str, nickname: str) -> str:
        """Give a device a human nickname (persisted). Use the friendly name or the old
        nickname to address it."""
        try:
            return _json(BRIDGE.set_nickname(device, nickname))
        except DeviceNotFound as error:
            return _json({"ok": False, "error": str(error)})

    @mcp.tool()
    def set_note(device: str, note: str) -> str:
        """Remember what a device is for, in the person's words (e.g. 'the lamp by my bed'). Shown with
        the device in list_devices so you know what you are switching. Persisted."""
        try:
            return _json(BRIDGE.set_note(device, note))
        except DeviceNotFound as error:
            return _json({"ok": False, "error": str(error)})

    # The two below are for the Smart home panel, not for her: OpenAmika's brain never offers them to the model.
    @mcp.tool()
    def permit_join(seconds: int = 254) -> str:
        """[panel only] Open zigbee2mqtt's pairing window for `seconds` (0 closes it) so a new device can join."""
        try:
            return _json(BRIDGE.set_permit_join(seconds))
        except RuntimeError as error:
            return _json({"ok": False, "error": str(error)})

    @mcp.tool()
    def discovery() -> str:
        """[panel only] Pairing state and the latest device join / interview events."""
        return _json(BRIDGE.discovery())

    @mcp.tool()
    def activate_scene(scene: str) -> str:
        """Apply a named scene from scenes.json (e.g. 'cozy' = several devices at once).
        Returns per-device results."""
        try:
            return _json(BRIDGE.activate_scene(scene))
        except ValueError as error:
            return _json({"ok": False, "error": str(error)})

    return mcp


def main():
    parser = argparse.ArgumentParser(prog="openamika-iot")
    parser.add_argument("--http", action="store_true",
                        help="serve Streamable HTTP instead of stdio (for a remote dongle host)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--accept-iot-disclaimer", metavar="VERSION", default=None,
                        help="confirm that you have read and accept OpenAmika's IoT safety statement "
                             f"(docs/IOT_DISCLAIMER.md, current version {IOT_DISCLAIMER_VERSION}); OpenAmika passes "
                             "it for you once it was accepted on the Tools page")
    args = parser.parse_args()
    problem = refusal(args.accept_iot_disclaimer)
    if problem:
        print(problem, file=sys.stderr)
        raise SystemExit(2)

    from .bridge import BRIDGE
    BRIDGE.start()
    mcp = build_server(args.host, args.port)
    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
