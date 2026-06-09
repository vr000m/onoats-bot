"""onoats transport implementations.

:class:`UnixSocketAudioInputTransport` is a pipecat input transport that reads
framed PCM16 LE / 16 kHz / mono audio from a unix domain socket
(``AUDIO_SOURCE=socket``). :class:`UnixSocketAudioTransport` is the input-only
``BaseTransport`` wrapper exposing ``.input()`` so it drops into
``_build_dual_pipeline`` in place of ``LocalAudioTransport`` with no pipeline
change.

This package is pure-Python (stdlib sockets + pipecat) and imports with no
native binary present, keeping the baseline / CI path native-free.
"""

from __future__ import annotations

from onoats.transports.socket_audio import (
    DEFAULT_MAX_BUFFERED_BYTES,
    BackpressurePolicy,
    HandshakeHeader,
    SocketHandshakeError,
    UnixSocketAudioInputTransport,
    UnixSocketAudioTransport,
    WIRE_VERSION,
    frame_size_bytes,
    parse_handshake,
)

__all__ = [
    "DEFAULT_MAX_BUFFERED_BYTES",
    "BackpressurePolicy",
    "HandshakeHeader",
    "SocketHandshakeError",
    "UnixSocketAudioInputTransport",
    "UnixSocketAudioTransport",
    "WIRE_VERSION",
    "frame_size_bytes",
    "parse_handshake",
]
