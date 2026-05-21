"""Binary frame format for multiplexed PTY data on the agent <-> master link.

Layout:
    [4 bytes BE: len(session_id)] [session_id utf-8] [data...]
"""

import struct
from typing import Tuple


def pack(session_id: str, data: bytes) -> bytes:
    sid = session_id.encode("utf-8")
    return struct.pack(">I", len(sid)) + sid + data


def unpack(frame: bytes) -> Tuple[str, bytes]:
    if len(frame) < 4:
        raise ValueError("frame too short")
    (sid_len,) = struct.unpack(">I", frame[:4])
    if len(frame) < 4 + sid_len:
        raise ValueError("frame truncated")
    session_id = frame[4 : 4 + sid_len].decode("utf-8")
    data = frame[4 + sid_len :]
    return session_id, data
