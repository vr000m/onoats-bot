"""onoats transport implementations.

Phase 1 ships :class:`UnixSocketAudioInputTransport`, a pipecat input transport
that reads framed PCM16 LE / 16 kHz / mono audio from a unix domain socket
(``AUDIO_SOURCE=socket``). The ``UnixSocketAudioTransport`` wrapper and the
``dual.py`` wiring land in Phase 2.

This package is pure-Python (stdlib sockets + pipecat) and imports with no
native binary present, keeping the baseline / CI path native-free.
"""

from __future__ import annotations

from onoats.transports.socket_audio import (
    BackpressurePolicy,
    HandshakeHeader,
    SocketHandshakeError,
    UnixSocketAudioInputTransport,
    WIRE_VERSION,
    frame_size_bytes,
    parse_handshake,
)

__all__ = [
    "BackpressurePolicy",
    "HandshakeHeader",
    "SocketHandshakeError",
    "UnixSocketAudioInputTransport",
    "WIRE_VERSION",
    "frame_size_bytes",
    "parse_handshake",
]
