#!/usr/bin/env python3
"""Standalone probe / publisher for the multicast_pubsub protocol.

Examples:
    python3 probe.py --topic test/temp                  # listen and decode
    python3 probe.py --topic test/temp --publish 42.5   # publish once and exit
    python3 probe.py --topic test/temp --scope link-local

Uses only the Python stdlib + the reference implementation in tests/unit/.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "unit"))
from reference import (  # noqa: E402
    DEFAULT_PORT,
    SCOPE_LINK_LOCAL,
    SCOPE_ORG_LOCAL,
    SCOPE_SITE_LOCAL,
    WireError,
    decode,
    encode,
    topic_to_group,
)


SCOPES = {
    "link-local": SCOPE_LINK_LOCAL,
    "site-local": SCOPE_SITE_LOCAL,
    "organization-local": SCOPE_ORG_LOCAL,
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--topic", required=True)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--scope", choices=sorted(SCOPES), default="link-local",
        help="IPv6 multicast scope nibble (default: link-local for loopback)",
    )
    p.add_argument(
        "--publish", metavar="TEXT",
        help="If set, publish TEXT once to the topic and exit.",
    )
    p.add_argument(
        "--iface", default=None,
        help="Interface name or index to use for IPv6 multicast (default: 0 = system default).",
    )
    p.add_argument(
        "--timeout", type=float, default=None,
        help="Stop listening after N seconds. Default: run forever.",
    )
    args = p.parse_args()

    scope = SCOPES[args.scope]
    group = topic_to_group(args.topic, scope)
    print(f"# topic={args.topic!r} -> group=[{group}]:{args.port}", file=sys.stderr)

    if args.publish is not None:
        payload = args.publish.encode("utf-8")
        pkt = encode(args.topic, payload, flags=0x01)  # FLAG_TEXT
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        if args.iface is not None:
            idx = socket.if_nametoindex(args.iface) if not args.iface.isdigit() else int(args.iface)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, idx)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
        sock.sendto(pkt, (str(group), args.port))
        print(f"sent {len(pkt)} bytes")
        return 0

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("::", args.port))
    idx = 0
    if args.iface is not None:
        idx = socket.if_nametoindex(args.iface) if not args.iface.isdigit() else int(args.iface)
    mreq = group.packed + struct.pack("@I", idx)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
    if args.timeout is not None:
        sock.settimeout(args.timeout)

    print(f"listening on [::]:{args.port} for group {group}...", file=sys.stderr)
    start = time.monotonic()
    while True:
        try:
            data, peer = sock.recvfrom(2048)
        except socket.timeout:
            return 0
        try:
            crc, flags, payload = decode(data)
        except WireError as e:
            print(f"[bad packet from {peer}]: {e}")
            continue
        text = ""
        if flags & 0x01:
            try:
                text = f" {payload.decode('utf-8')!r}"
            except UnicodeDecodeError:
                text = ""
        print(
            f"+{time.monotonic() - start:.3f}s from {peer}: "
            f"crc={crc:08x} flags={flags:02x} payload({len(payload)} B)={payload.hex()}{text}"
        )


if __name__ == "__main__":
    sys.exit(main())
