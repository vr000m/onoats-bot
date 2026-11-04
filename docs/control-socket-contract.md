# Recorder control socket contract

Version 1 replaces PID signalling for normal `onoats flush` and `onoats stop`
commands. Direct SIGUSR1/SIGTERM inputs remain supported for terminals and
service managers, but the CLI never falls back to them.

| Code constant | Wire value |
|---|---:|
| `CONTROL_PROTOCOL_VERSION` | `1` |
| `CONTROL_MAX_REQUEST_BYTES` | `4096` |
| `CONTROL_MAX_RESPONSE_BYTES` | `4096` |
| `CONTROL_SOCKET_FILENAME` | `onoats-control.sock` |

## Endpoint and ownership

The recorder binds `<data_dir>/.active/onoats-control.sock` before publishing
its PID and running status. `.active` is mode `0700` and the socket is `0600`.
The server records the bound socket's device/inode. Cleanup atomically moves the
public entry into a private same-directory quarantine, verifies the moved inode,
and removes only that socket; a raced foreign entry is restored without
overwriting a successor or retained in quarantine. Shutdown stops acceptance
and settles accepted commands before removing the socket, writing stopped
status, and exact-record PID teardown.

On macOS every accepted connection must provide Darwin `LOCAL_PEERCRED`. The
server requires a complete `xucred` with `cr_version == XUCRED_VERSION`, a valid
group count, and `cr_uid == geteuid()`. Missing, malformed, unsupported, or
wrong-UID credentials are rejected. Filesystem modes are defence in depth.

## Wire format

One connection carries exactly one UTF-8 JSON request terminated by one newline,
then EOF. The complete request is at most 4096 bytes. Its exact fields are:

```json
{"version":1,"command":"flush","pid":123,"start_epoch":1790000000.25}
```

- `version` is integer `1`.
- `command` is exactly `flush` or `stop`.
- `pid` is a positive integer.
- `start_epoch` is the recorder's required positive finite epoch from the PID
  record. `pid` and `start_epoch` must both match the server generation.

The response is one newline-terminated JSON object, at most 4096 bytes, with
exact fields `version`, `ok`, `code`, and `message`. `ok=true` is an
acknowledgement of command-specific acceptance, not proof that the recorder has
exited. A dropped or malformed acknowledgement is failure.

Malformed JSON, absent/partial/extra identity fields, multiple messages,
unterminated or oversized input, an unsupported version, an unsupported command, generation
mismatch, and unauthorised peers all fail closed. A missing, legacy, replaced,
or incompatible endpoint is never converted into a PID signal: there is **no PID-signal fallback**.

## Serialized command state

One recorder-loop lock serializes accepted commands:

- A Flush that acquires the lock first completes continuation rotation before
  its success acknowledgement.
- A Stop that acquires first marks graceful shutdown and sets the shutdown event
  before acknowledgement.
- A duplicate Stop is idempotently acknowledged.
- Any Flush acquiring the lock after Stop acceptance is rejected as `stopping`.

Therefore simultaneous Stop/Flush is resolved solely by lock acquisition order,
and an acknowledgement always describes an action already accepted by that same
recorder generation.

In shorthand: **flush-first** means rotation completes before Stop; **stop-first**
means graceful shutdown is marked before the later Flush is rejected.

## PID cleanup

Every stale or normal PID removal uses quarantine-and-verify cleanup: it rereads
and compares the complete captured record (PID, command-line fingerprint, and
start epoch), atomically moves the public entry aside, and removes only an exact
match. Unreadable, malformed, absent, foreign, replaced, and same-PID/new-epoch
successor records are preserved.
