"""Parity guards for the documented recorder control protocol."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import onoats.control_socket as control


def test_atomic_noreplace_has_one_production_owner() -> None:
    import onoats._atomic_fs as atomic_fs
    import onoats.ui_telemetry as ui_telemetry

    owner = inspect.getsource(atomic_fs)
    assert "renameat2" in owner and "renameatx_np" in owner
    control_source = inspect.getsource(control)
    telemetry_source = inspect.getsource(ui_telemetry)
    assert "ctypes.CDLL" not in control_source
    assert "ctypes.CDLL" not in telemetry_source
    package_import = re.compile(
        r"(?m)^\s*(?:from\s+onoats(?:\.|\s)|import\s+onoats(?:\.|\s|$))"
    )
    assert package_import.search(control_source) is None
    assert package_import.search(telemetry_source) is None


DOC = Path(__file__).resolve().parents[1] / "docs" / "control-socket-contract.md"


def _doc() -> str:
    assert DOC.is_file(), "the versioned control-channel contract must be documented"
    return DOC.read_text(encoding="utf-8")


def test_documented_protocol_constants_match_code() -> None:
    doc = _doc()
    expected = {
        "CONTROL_PROTOCOL_VERSION": control.CONTROL_PROTOCOL_VERSION,
        "CONTROL_MAX_REQUEST_BYTES": control.CONTROL_MAX_REQUEST_BYTES,
        "CONTROL_MAX_RESPONSE_BYTES": control.CONTROL_MAX_RESPONSE_BYTES,
        "CONTROL_SOCKET_FILENAME": control.CONTROL_SOCKET_FILENAME,
    }
    for name, value in expected.items():
        assert re.search(
            rf"(?m)`{re.escape(name)}`[^\n]*`{re.escape(str(value))}`", doc
        ), f"{name}={value!r} is not mirrored by the contract"


def test_documented_exact_shapes_and_commands_match_code() -> None:
    doc = _doc()
    for field in control.CONTROL_REQUEST_FIELDS:
        assert re.search(rf"`{re.escape(field)}`", doc), field
    for field in control.CONTROL_RESPONSE_FIELDS:
        assert re.search(rf"`{re.escape(field)}`", doc), field
    for command in control.CONTROL_COMMANDS:
        assert re.search(rf"`{re.escape(command)}`", doc), command

    source = inspect.getsource(control.ControlServer._handle)
    assert "set(request) != set(CONTROL_REQUEST_FIELDS)" in source
    assert "set(payload)" not in source  # responses are owned by _encoded_response
    client = inspect.getsource(control.send_control_command)
    assert "set(payload) != set(CONTROL_RESPONSE_FIELDS)" in client


def test_protocol_version_checks_are_strict_integer_guards() -> None:
    """Removing either type check must re-admit JSON true or 1.0."""
    server = inspect.getsource(control.ControlServer._handle)
    client = inspect.getsource(control.send_control_command)
    assert 'type(request["version"]) is not int' in server
    assert 'type(payload["version"]) is not int' in client


def test_contract_documents_every_fail_closed_rejection_class() -> None:
    doc = _doc().lower()
    rejection_terms = {
        "missing identity": ("absent", "missing"),
        "partial identity": ("partial", "incomplete"),
        "generation mismatch": ("mismatch", "generation"),
        "malformed json": ("malformed",),
        "oversized request": ("oversized", "byte limit"),
        "multiple messages": ("multi-message", "multiple message", "exactly one"),
        "unsupported version": ("unsupported version", "unsupported protocol"),
        "unsupported command": ("unsupported command",),
        "missing peer credentials": ("credential", "local_peercred"),
        "wrong peer uid": ("same uid", "effective uid", "cr_uid"),
        "stopping flush": ("flush after stop", "later flush", "stopping"),
        "missing endpoint": ("missing", "legacy"),
        "no signal fallback": ("no pid-signal fallback", "no pid signal fallback"),
    }
    for rule, alternatives in rejection_terms.items():
        assert any(term in doc for term in alternatives), (
            f"control contract does not document {rule}"
        )


def test_contract_and_code_pin_peer_policy_and_acknowledgement_points() -> None:
    doc = _doc().lower()
    assert "local_peercred" in doc
    assert "xucred" in doc
    assert "cr_uid" in doc
    assert "so_peercred" in doc
    assert "kernel-enforced acl" in doc
    assert "fail closed" in doc

    assert "flush" in doc and "rotation" in doc and "acknowledg" in doc
    assert "stop" in doc and "graceful" in doc
    assert "duplicate stop" in doc and "idempotent" in doc
    assert "lock" in doc and "flush-first" in doc and "stop-first" in doc

    peer_source = inspect.getsource(control._darwin_peer_uid)
    assert "getsockopt" in peer_source
    assert "_XUCRED_VERSION" in peer_source
    platform_source = inspect.getsource(control._platform_peer_uid)
    assert "_linux_peer_uid" in platform_source
    assert 'sys.platform == "win32"' in platform_source
    assert "os.geteuid()" in inspect.getsource(control.ControlServer._handle)


def test_contract_pins_owned_endpoint_cleanup_and_successor_safety() -> None:
    doc = _doc().lower()
    assert "quarantine" in doc
    assert "atomically moves" in doc
    assert "inode" in doc
    assert "successor" in doc or "replacement" in doc
    close_source = inspect.getsource(control.ControlServer.close)
    cleanup_source = inspect.getsource(control._quarantine_and_remove)
    identity_source = inspect.getsource(control._EndpointIdentity.matches)
    capture_source = inspect.getsource(control._capture_endpoint_identity)
    assert "st_ino" in identity_source and "st_dev" in identity_source
    assert "O_PATH" in capture_source and "os.open" in capture_source
    assert "pins the captured node" in doc
    assert cleanup_source.index("os.rename") < cleanup_source.index(
        "quarantine_path.unlink"
    )
    assert close_source.index("wait_closed") < close_source.index(
        "_quarantine_and_remove"
    )


def test_control_callbacks_and_both_entrypoints_share_force_exit_contract() -> None:
    signature = inspect.signature(control.ControlServer.__init__)
    assert "bool" in str(signature.parameters["on_flush"].annotation)
    close = inspect.signature(control.ControlServer.close)
    assert "force_exit_event" in close.parameters

    import onoats.__main__ as single
    import onoats.dual as dual

    for source in (
        inspect.getsource(single.run_onoats),
        inspect.getsource(dual.run_onoats_dual),
    ):
        assert "close(force_exit_event=force_exit_event)" in source
