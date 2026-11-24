"""Them-channel diarization timeline: correlation clock, coverage ring, label policy.

Leaf module: no ``onoats`` imports and no ``mlx_audio`` import (a third-party
import of ``stt_server.protocol`` is allowed and optional). Both the upstream
feeder/worker and the STT service import from here without either importing
the other, and ``import onoats.dual`` stays native-free.

It is the **single owner** of:

* **The correlation clock.** The clock is the count of 16 kHz mono PCM16
  samples seen since ``StartFrame`` (:class:`SampleCounter`). The feeder and
  ``WebSocketSTTService`` each count independently with this one helper; that
  is valid only because the feeder is a pure pass-through (no drop, no
  re-chunk between them). The clock is monotonic for the session and is NOT
  reset by ``onoats flush``.
* **:class:`DiarizationTimeline`** - a session-scoped, thread-safe,
  ring-bounded record of per-frame speaker probabilities. The **worker is
  the sole coverage writer** (``record``, ``record_gap``, ``set_slot_count``,
  ``mark_flushed``); the event loop otherwise only reads (``label_for``,
  ``coverage_high_water``, ``is_ready``, ``is_degraded``, ``is_flushed``,
  ``add_progress_listener``). ``mark_degraded`` is the one idempotent,
  thread-safe flag either thread may set: the worker on its own failures, the
  loop from the labeler (stall guard, release-task fail-open) and the feeder
  (worker start failure, worker join timeout).
* **The label policy** (:meth:`DiarizationTimeline.label_for`) and its named
  constants.

Slot -> ``S{n}`` mapping happens in ``record`` (worker thread), in the order
each model slot first crosses ``ACTIVITY_THRESHOLD`` in the session, so the
loop-side ``label_for`` never mutates mapping state. Numbers come from one
monotonic counter and are never reused: a slot idle for more than
``SLOT_IDLE_REMINT_SEC`` (overridable per timeline) is given a fresh number on
its next active frame, so a label does not silently move to a different speaker
if the model recycles the slot. A slot therefore has a list of generations ``(start_sample, n)``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from loguru import logger

# --- Clock contract ---------------------------------------------------------

SAMPLE_RATE_HZ = 16_000
BYTES_PER_SAMPLE = 2  # PCM16 mono
#: Model output hop: one probability row per 10 ms (160 samples at 16 kHz).
FRAME_SAMPLES = 160

# --- Policy constants -------------------------------------------------------

#: Per-frame probability at/above which a slot counts as active.
ACTIVITY_THRESHOLD = 0.5
#: When the top-scored slot is not the sole OVERLAP_MIN_FRACTION candidate (no
#: candidate at all - sparse, scattered activity - or a lower-scored candidate),
#: it must lead the runner-up's activity-weighted score by at least this much
#: or the window is left unlabelled.
DOMINANT_MARGIN = 0.15
#: Two+ slots each active for >= this fraction of the covered window => overlap.
OVERLAP_MIN_FRACTION = 0.2
#: Minimum fraction of the window that must be recorded (not GAP/evicted/pending).
MIN_COVERAGE_FRACTION = 0.5
#: Max time the labeler holds a TranscriptionFrame waiting for coverage.
#: `low` preset lag (~1.04 s) + margin. The dev plan's ``LATENCY_BUDGET_SEC``
#: (2.0 s) is this + margin - a plan acceptance threshold, not a code constant.
HOLD_SEC = 1.5
#: A model slot idle (no active frame) for longer than this is given a fresh
#: label on its next active frame. This errs toward splitting one person in two
#: (safe) rather than letting a recycled slot carry an old speaker's label; it
#: cannot catch a swap while both speakers are active. 1200 s because a live
#: 4-person call (2026-10-12) split every returning speaker at the earlier 300 s,
#: after silences of 336-946 s; ``[stt].diarization_idle_remint_sec`` overrides it.
SLOT_IDLE_REMINT_SEC = 1200.0


def effective_idle_remint_sec(idle_remint_sec: float | None) -> float:
    """The re-mint window a timeline built with *idle_remint_sec* will use.

    The one owner of the ``None`` -> default rule (``is None``, not truthiness):
    ``DiarizationTimeline`` builds from it and ``dual`` writes it into the
    ``session_meta`` header, so the header can never name another window.
    """
    return SLOT_IDLE_REMINT_SEC if idle_remint_sec is None else float(idle_remint_sec)


#: Queue / segment-serialisation slack added to the ring capacity.
RING_HEADROOM_SEC = 10.0
#: How long after the labeler starts the worker may still be loading the model
#: (no coverage is possible yet) before a no-progress expiry counts toward the
#: labeler's stall guard. A cold model load is not a hung ``feed()``.
MODEL_LOAD_GRACE_SEC = 60.0
#: Decode-timeout factor; ``websocket_stt_service`` imports it for
#: ``max(_DECODE_TIMEOUT_SECONDS, DECODE_TIMEOUT_RATIO * audio_seconds)``.
DECODE_TIMEOUT_RATIO = 1.5

try:  # the STT server's own cap on one uncommitted turn
    from stt_server.protocol import MAX_UNCOMMITTED_SECONDS as _SERVER_MAX_TURN_SEC

    MAX_TURN_SEC: float = float(_SERVER_MAX_TURN_SEC)
except Exception:  # pragma: no cover - documented literal fallback
    MAX_TURN_SEC = 300.0

#: Ring capacity in seconds: a maximum-length turn (plus its decode time and the
#: hold window) cannot be evicted before its frame arrives.
RING_CAPACITY_SEC: float = (
    MAX_TURN_SEC * (1 + DECODE_TIMEOUT_RATIO) + HOLD_SEC + RING_HEADROOM_SEC
)

Listener = Callable[[], None]


def segmented_stt_orders_spans() -> bool:
    """True when ``SegmentedSTTService`` hands segments to ``run_stt`` in VAD-stop order.

    The STT's span queue (one entry per VAD-stop, popped at the head of
    ``run_stt``) is only sound if segments reach ``run_stt`` in the order their
    VAD-stops were seen. Two shapes guarantee that: pipecat 1.3.0-1.8.x await
    ``run_stt`` inline in the stop handler, once and unconditionally
    (:func:`_inline_handler_runs_stt_once`); from 1.9.0 the handler puts the
    segment on ``_segment_queue`` (1.17 puts an ``(audio, number)`` tuple and
    its reader hands the audio to ``_push_segment_frames``) and ONE
    ``_segment_task_handler`` task takes them off one at a time. The queue
    shape is only in order if the whole
    class agrees (:func:`_segment_queue_is_one_fifo_consumer`): the queue is
    built once as a plain ``asyncio.Queue()`` (not LIFO / priority),
    ``start`` creates exactly one handler task, the queue's only reader, each
    VAD-stop puts exactly one segment, that reader is one loop that hands
    each segment to the class's only ``run_stt`` site until the ``None``
    sentinel, and nothing in its loop can skip, repeat or swap one. Anything
    else (a concurrent, re-ordering, one-shot or skipping design) could
    strand a span or pair it with another segment - a wrong label rather
    than a missing one - so the startup preflight refuses it.

    Fails closed: an unreadable source or an unrecognised shape is "not
    ordered". Third-party import only (lazy), so this stays a leaf helper.
    """
    import inspect

    from pipecat.services.stt_service import SegmentedSTTService

    handler = getattr(SegmentedSTTService, "_handle_user_stopped_speaking", None)
    if handler is None:
        return False
    try:
        src = inspect.getsource(handler)
    except (OSError, TypeError):
        return False
    if getattr(SegmentedSTTService, "_segment_task_handler", None) is None:
        if src.count("self.run_stt(") != 1 or "queue" in src.lower():
            return False
        import ast
        import textwrap

        try:
            tree = ast.parse(textwrap.dedent(src))
        except SyntaxError:
            return False
        return _inline_handler_runs_stt_once(tree)
    return _segment_queue_is_one_fifo_consumer(SegmentedSTTService)


def _is_self_attr(node: object, attr: str) -> bool:
    import ast

    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _segment_queue_is_one_fifo_consumer(cls: type) -> bool:
    """True iff ``cls`` builds ``_segment_queue`` as ONE FIFO with ONE reader task.

    Parses every method defined on ``cls`` (the class that owns the queue) and
    requires, exactly as pipecat 1.9-1.17 write it:

    - the queue is assigned once, in ``__init__``, as ``asyncio.Queue()`` with
      no arguments (a ``LifoQueue`` / ``PriorityQueue`` / factory call / a
      second assignment is not order-preserving or not recognisable);
    - ``self._segment_task_handler`` is referenced once in the class, as the
      top-level ``start`` statement
      ``self._segment_task = self.create_task(self._segment_task_handler())``
      (not in a loop, not twice, not elsewhere);
    - ``self._segment_queue.get``/``get_nowait`` is called once, inside
      ``_segment_task_handler`` (no second reader);
    - ``self._segment_queue.put``/``put_nowait`` carries a segment only in
      ``_handle_user_stopped_speaking``, exactly once
      (:func:`_stop_handler_puts_one_segment`); every other put is the
      ``None`` finish sentinel (``stop``);
    - the reader drains one-for-one (:func:`_reader_hand_off`);
    - ``self.run_stt`` is referenced once in the class: in the reader
      (1.9-1.12), or in ``_push_segment_frames`` (1.17,
      :func:`_helper_runs_stt_once`) when the reader's hand-off is the
      helper's only reference.

    Fails closed on an unreadable/unparseable method or a ``"_segment_queue"``
    / ``"run_stt"`` / ``"_push_segment_frames"`` string (a ``getattr`` /
    ``setattr`` the walk cannot follow).
    """
    import ast
    import inspect
    import textwrap

    methods: dict[str, ast.AST] = {}
    for name, attr in vars(cls).items():
        fn = attr.fget if isinstance(attr, property) else attr
        fn = getattr(fn, "__func__", fn)  # staticmethod / classmethod
        if not inspect.isfunction(fn):
            continue
        try:
            methods[name] = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        except (OSError, TypeError, SyntaxError):
            return False

    queue_assigns: list[tuple[str, ast.expr | None]] = []
    handler_refs: list[str] = []
    readers: list[str] = []
    segment_puts: list[str] = []
    for name, tree in methods.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in (
                "_segment_queue",
                "run_stt",
                _SEGMENT_PUSH_HELPER,
            ):
                return False
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) or isinstance(
                node, ast.NamedExpr
            ):
                targets, value = [node.target], node.value
            else:
                targets, value = [], None
            for target in targets:
                if any(_is_self_attr(t, "_segment_queue") for t in ast.walk(target)):
                    queue_assigns.append((name, value))
            if _is_self_attr(node, "_segment_task_handler"):
                handler_refs.append(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "get_nowait")
                and _is_self_attr(node.func.value, "_segment_queue")
            ):
                readers.append(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("put", "put_nowait")
                and _is_self_attr(node.func.value, "_segment_queue")
            ):
                sentinel = (
                    len(node.args) == 1
                    and not node.keywords
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is None
                )
                if name == "_handle_user_stopped_speaking" and not sentinel:
                    segment_puts.append(name)
                elif not sentinel:
                    return False  # a segment producer other than the VAD stop

    if len(queue_assigns) != 1:
        return False
    where, value = queue_assigns[0]
    if not (
        where == "__init__"
        and isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "Queue"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "asyncio"
        and not value.args
        and not value.keywords
    ):
        return False
    if readers != ["_segment_task_handler"] or handler_refs != ["start"]:
        return False
    if segment_puts != ["_handle_user_stopped_speaking"]:
        return False
    if not _stop_handler_puts_one_segment(methods["_handle_user_stopped_speaking"]):
        return False
    hand_off = _reader_hand_off(methods["_segment_task_handler"])
    if hand_off is None:
        return False
    # One ``run_stt`` site for the whole class, whichever reader shape: in
    # the reader itself (1.9-1.12), or in the helper the reader alone
    # references (1.17). Any other site - a second call, a bare reference
    # handed out as a callback, the helper calling itself - would run a
    # segment outside the one in-order task and pop another segment's span.
    run_sites = [
        name
        for name, tree in methods.items()
        for node in ast.walk(tree)
        if _is_self_attr(node, "run_stt")
    ]
    helper_sites = [
        name
        for name, tree in methods.items()
        for node in ast.walk(tree)
        if _is_self_attr(node, _SEGMENT_PUSH_HELPER)
    ]
    if hand_off == "run_stt":
        if run_sites != ["_segment_task_handler"] or helper_sites:
            return False
    elif not (
        run_sites == [_SEGMENT_PUSH_HELPER]
        and helper_sites == ["_segment_task_handler"]
        and _helper_runs_stt_once(methods[_SEGMENT_PUSH_HELPER])
    ):
        return False

    def _is_the_one_task(stmt: ast.stmt) -> bool:
        call = stmt.value if isinstance(stmt, ast.Assign) else None
        return (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and _is_self_attr(stmt.targets[0], "_segment_task")
            and isinstance(call, ast.Call)
            and _is_self_attr(call.func, "create_task")
            and len(call.args) == 1
            and not call.keywords
            and isinstance(call.args[0], ast.Call)
            and _is_self_attr(call.args[0].func, "_segment_task_handler")
            and not call.args[0].args
            and not call.args[0].keywords
        )

    start = methods["start"].body[0]
    if not isinstance(start, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return sum(_is_the_one_task(stmt) for stmt in start.body) == 1


def _function_def(tree: object) -> object | None:
    import ast

    body = getattr(tree, "body", None)
    if not body or not isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return body[0]


def _is_queue_call(node: object, method: str) -> bool:
    import ast

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
        and _is_self_attr(node.func.value, "_segment_queue")
    )


def _is_usable_guard(stmt: object) -> bool:
    """``if not self.is_usable:`` with no ``else`` (the one early exit pipecat 1.9+ has)."""
    import ast

    return (
        isinstance(stmt, ast.If)
        and isinstance(stmt.test, ast.UnaryOp)
        and isinstance(stmt.test.op, ast.Not)
        and _is_self_attr(stmt.test.operand, "is_usable")
        and not stmt.orelse
    )


def _inline_handler_runs_stt_once(tree: object) -> bool:
    """True iff the VAD-stop handler awaits ``run_stt`` once, on every stop.

    The pipecat 1.3-1.8 shape: one top-level
    ``await self.process_generator(self.run_stt(<audio>))`` statement (not under
    a branch, loop or ``try``) and no ``return`` / ``raise`` anywhere else (an
    ``if not self.is_usable:`` guard excepted, as in the queue shape). The STT
    appends one span per VAD stop, so a handler that can leave without calling
    ``run_stt``, or calls it conditionally, would strand a span and pair every
    later span with the wrong segment - a wrong label rather than a missing one.
    The exact-one-call count alone cannot tell those from the sound shape.
    """
    import ast

    fn = _function_def(tree)
    if fn is None:
        return False

    def _is_run_stmt(stmt: ast.stmt) -> bool:
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Await)
            and isinstance(stmt.value.value, ast.Call)
        ):
            return False
        call = stmt.value.value
        return (
            _is_self_attr(call.func, "process_generator")
            and len(call.args) == 1
            and not call.keywords
            and isinstance(call.args[0], ast.Call)
            and _is_self_attr(call.args[0].func, "run_stt")
        )

    if sum(_is_run_stmt(stmt) for stmt in fn.body) != 1:
        return False
    run_calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _is_self_attr(n.func, "run_stt")
    ]
    if len(run_calls) != 1:
        return False
    for stmt in fn.body:
        if _is_usable_guard(stmt):
            continue
        if any(isinstance(n, (ast.Return, ast.Raise)) for n in ast.walk(stmt)):
            return False
    return True


def _stop_handler_puts_one_segment(tree: object) -> bool:
    """True iff the VAD-stop handler puts its segment on EVERY stop it does not drop.

    The STT appends one span per VAD-stop and skips only when ``is_usable`` is
    false (mirroring pipecat 1.9-1.17, which drops that segment). So the put
    must be one top-level ``await self._segment_queue.put(<segment>)``
    statement (not under a branch or loop), and every ``return`` / ``raise``
    in the handler must sit in an ``if not self.is_usable:`` block - any
    other early exit would leave the STT's span without a segment.
    """
    import ast

    fn = _function_def(tree)
    if fn is None:
        return False

    def _is_put_stmt(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Await)
            and _is_queue_call(stmt.value.value, "put")
            and len(stmt.value.value.args) == 1
            and not stmt.value.value.keywords
        )

    if sum(_is_put_stmt(stmt) for stmt in fn.body) != 1:
        return False
    for stmt in fn.body:
        if _is_usable_guard(stmt):
            continue
        if any(isinstance(n, (ast.Return, ast.Raise)) for n in ast.walk(stmt)):
            return False
    return True


#: Attributes whose use outside their one sanctioned site could reorder,
#: skip or repeat a segment's trip to ``run_stt``.
_ORDER_ATTRS = ("run_stt", "_segment_queue", "_segment_task_handler")

# pipecat 1.17 moved the reader's ``run_stt`` call into this method.
_SEGMENT_PUSH_HELPER = "_push_segment_frames"


def _frame_nodes(nodes: list, *, loops: bool = False):
    """Every AST node that runs in the same frame as ``nodes``.

    Like ``ast.walk`` but it does not enter the body of a nested ``def``,
    ``async def`` or ``lambda`` (its decorators, defaults and annotations are
    still visited: they run in this frame). A ``return`` in a local predicate
    then does not count as the enclosing function's exit. With
    ``loops=True`` it also skips the body (not the ``else``) of every loop, so
    a ``break``/``continue`` found is one that targets an enclosing loop.
    Classes and comprehensions are entered: fail closed on what they bind.
    """
    import ast

    todo = list(nodes)
    while todo:
        node = todo.pop()
        yield node
        skip_body = isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ) or (loops and isinstance(node, (ast.For, ast.AsyncFor, ast.While)))
        for field, value in ast.iter_fields(node):
            if skip_body and field == "body":
                continue
            if isinstance(value, ast.AST):
                todo.append(value)
            elif isinstance(value, list):
                todo.extend(v for v in value if isinstance(v, ast.AST))


def _binds_any(nodes: list, names: set[str]) -> bool:
    """True if ``nodes`` (re)bind or delete any of ``names`` in their frame.

    Covers every binding form (assignment and walrus targets, ``for`` /
    ``with`` / ``except ... as`` / ``import ... as`` / ``match`` captures,
    ``def`` / ``class`` names), plus a ``global`` / ``nonlocal`` of one of
    them anywhere, even inside a nested function (it rebinds this frame's
    name from there).
    """
    import ast

    def _bound(node: ast.AST):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield node.id
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name
        elif isinstance(node, ast.alias):
            yield (node.asname or node.name).split(".")[0]
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name

    for node in _frame_nodes(nodes):
        if any(name in names for name in _bound(node)):
            return True
    return any(
        isinstance(node, (ast.Global, ast.Nonlocal))
        and any(name in names for name in node.names)
        for top in nodes
        for node in ast.walk(top)
    )


def _is_inert(node: object, protected: set[str]) -> bool:
    """True if ``node`` cannot change which segment reaches ``run_stt`` next.

    It never leaves the reader's loop or function (no ``return``, ``raise``,
    ``yield``, or ``break``/``continue`` aimed at that loop), never rebinds
    the loop's flag / item / audio names, and never touches the queue,
    ``run_stt`` or the hand-off helper (even from a nested function: a
    callback is another way to run them).
    """
    import ast

    if any(
        isinstance(n, (ast.Return, ast.Raise, ast.Yield, ast.YieldFrom))
        for n in _frame_nodes([node])
    ):
        return False
    if any(
        isinstance(n, (ast.Break, ast.Continue))
        for n in _frame_nodes([node], loops=True)
    ):
        return False
    if _binds_any([node], protected):
        return False
    return not any(
        _is_self_attr(n, attr)
        for n in ast.walk(node)
        for attr in (*_ORDER_ATTRS, _SEGMENT_PUSH_HELPER)
    )


def _without_docstring(body: list) -> list:
    import ast

    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _reader_hand_off(tree: object) -> str | None:
    """How ``_segment_task_handler`` hands each queued segment to ``run_stt``.

    Returns ``"run_stt"`` (pipecat 1.9-1.12 awaits
    ``self.process_generator(self.run_stt(<audio>))`` itself), ``"helper"``
    (1.17 awaits ``self._push_segment_frames(<audio>)``, which the caller must
    prove runs ``run_stt`` once: :func:`_helper_runs_stt_once`), or ``None``
    when the reader is not provably one-for-one. The body must be exactly::

        running = True
        while running:
            item = await self._segment_queue.get()
            running = item is not None
            if item:
                audio, self._transcribing_segment = item  # optional (1.17)
                try:
                    <hand-off of audio>               # the try's FIRST statement
                except Exception as e:
                    ...                               # inert
                ...                                   # inert
            ...                                       # inert

    (an optional docstring first). The sentinel is the only exit; the unpack,
    if present, directly precedes the ``try`` (so ``audio`` is always this
    item's); nothing runs between the ``try`` and the hand-off (a caught
    failure there would skip a segment); ``try`` has no ``else``/``finally``;
    and every other statement - the handlers and anything after the hand-off
    at each level - is inert (:func:`_is_inert`). A nested function or loop
    is judged by what runs in this frame, so a local predicate's ``return``
    or an inner loop's ``continue`` neither passes nor fails the reader.

    ``if <item>:`` skips an empty segment without calling ``run_stt``. That is
    unreachable for ``WebSocketSTTService``: it keeps pipecat's default
    ``wants_wav_segments`` (True), so every segment is a non-empty WAV (and
    1.17's tuple is always truthy).
    """
    import ast

    fn = _function_def(tree)
    if not isinstance(fn, ast.AsyncFunctionDef):
        return None
    body = _without_docstring(fn.body)
    if len(body) != 2:
        return None
    init, loop = body
    if not (
        isinstance(init, ast.Assign)
        and len(init.targets) == 1
        and isinstance(init.targets[0], ast.Name)
        and isinstance(init.value, ast.Constant)
        and init.value.value is True
    ):
        return None
    flag = init.targets[0].id
    if not (
        isinstance(loop, ast.While)
        and isinstance(loop.test, ast.Name)
        and loop.test.id == flag
        and not loop.orelse
        and len(loop.body) >= 3
    ):
        return None

    get, sentinel, guard, *loop_tail = loop.body
    # ``<item> = await self._segment_queue.get()``
    if not (
        isinstance(get, ast.Assign)
        and len(get.targets) == 1
        and isinstance(get.targets[0], ast.Name)
        and get.targets[0].id != flag
        and isinstance(get.value, ast.Await)
        and _is_queue_call(get.value.value, "get")
        and not get.value.value.args
        and not get.value.value.keywords
    ):
        return None
    item = get.targets[0].id
    # ``<flag> = <item> is not None``
    if not (
        isinstance(sentinel, ast.Assign)
        and len(sentinel.targets) == 1
        and isinstance(sentinel.targets[0], ast.Name)
        and sentinel.targets[0].id == flag
        and isinstance(sentinel.value, ast.Compare)
        and isinstance(sentinel.value.left, ast.Name)
        and sentinel.value.left.id == item
        and len(sentinel.value.ops) == 1
        and isinstance(sentinel.value.ops[0], ast.IsNot)
        and isinstance(sentinel.value.comparators[0], ast.Constant)
        and sentinel.value.comparators[0].value is None
    ):
        return None
    # ``if <item>:`` with no ``else``
    if not (
        isinstance(guard, ast.If)
        and isinstance(guard.test, ast.Name)
        and guard.test.id == item
        and not guard.orelse
        and guard.body
    ):
        return None

    guarded = guard.body
    audio = item
    first = guarded[0]
    # pipecat 1.17: ``audio, self._transcribing_segment = <item>``
    if (
        isinstance(first, ast.Assign)
        and len(first.targets) == 1
        and isinstance(first.targets[0], ast.Tuple)
        and len(first.targets[0].elts) == 2
        and isinstance(first.value, ast.Name)
        and first.value.id == item
    ):
        head, number = first.targets[0].elts
        if not (
            isinstance(head, ast.Name)
            and head.id not in (flag, item)
            and (
                (
                    isinstance(number, ast.Name)
                    and number.id not in (flag, item, head.id)
                )
                or (
                    isinstance(number, ast.Attribute)
                    and isinstance(number.value, ast.Name)
                    and number.value.id == "self"
                    and number.attr not in (*_ORDER_ATTRS, _SEGMENT_PUSH_HELPER)
                )
            )
        ):
            return None
        audio = head.id
        guarded = guarded[1:]
    if not guarded:
        return None
    attempt, *guard_tail = guarded
    if not (
        isinstance(attempt, ast.Try)
        and attempt.handlers
        and not attempt.orelse
        and not attempt.finalbody
    ):
        return None
    hand_off, *try_tail = attempt.body

    def _is_audio_arg(call: object) -> bool:
        return (
            isinstance(call, ast.Call)
            and len(call.args) == 1
            and not call.keywords
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == audio
        )

    if not (
        isinstance(hand_off, ast.Expr)
        and isinstance(hand_off.value, ast.Await)
        and isinstance(hand_off.value.value, ast.Call)
    ):
        return None
    call = hand_off.value.value
    if (
        _is_self_attr(call.func, "process_generator")
        and len(call.args) == 1
        and not call.keywords
        and isinstance(call.args[0], ast.Call)
        and _is_self_attr(call.args[0].func, "run_stt")
        and _is_audio_arg(call.args[0])
    ):
        kind = "run_stt"
    elif _is_self_attr(call.func, _SEGMENT_PUSH_HELPER) and _is_audio_arg(call):
        kind = "helper"
    else:
        return None

    protected = {flag, item, audio}
    rest = [*attempt.handlers, *try_tail, *guard_tail, *loop_tail]
    if not all(_is_inert(node, protected) for node in rest):
        return None
    return kind


def _helper_runs_stt_once(tree: object) -> bool:
    """True iff ``_push_segment_frames(audio)`` iterates ``run_stt(audio)`` once, unconditionally.

    The pipecat 1.17 shape::

        async def _push_segment_frames(self, audio):
            \"\"\"...\"\"\"
            held: TranscriptionFrame | None = None
            async for frame in self.run_stt(audio):
                ...

    Exactly two plain parameters; one top-level
    ``async for <x> in self.run_stt(<audio>)`` (no ``else``, not under a
    branch, loop or ``try``) and no other ``run_stt`` reference; ``audio``
    never rebound anywhere in the helper (so the segment iterated is the one
    the reader handed over); no reference to the helper itself (a retry would
    pop another segment's span); and before the loop only a docstring,
    constant assignments to other names, ``pass`` or a plain local ``def``
    (no decorators, defaults or annotations, which would run here): nothing
    that can raise or return before ``run_stt`` pops this segment's span.
    """
    import ast

    fn = _function_def(tree)
    if not isinstance(fn, ast.AsyncFunctionDef):
        return False
    args = fn.args
    if (
        args.posonlyargs
        or args.vararg
        or args.kwonlyargs
        or args.kwarg
        or args.defaults
        or len(args.args) != 2
        or args.args[0].arg != "self"
    ):
        return False
    audio = args.args[1].arg

    def _is_run_loop(stmt: ast.stmt) -> bool:
        it = stmt.iter if isinstance(stmt, ast.AsyncFor) else None
        return (
            isinstance(it, ast.Call)
            and _is_self_attr(it.func, "run_stt")
            and len(it.args) == 1
            and not it.keywords
            and isinstance(it.args[0], ast.Name)
            and it.args[0].id == audio
            and not stmt.orelse
        )

    loops = [i for i, stmt in enumerate(fn.body) if _is_run_loop(stmt)]
    if len(loops) != 1:
        return False
    if sum(_is_self_attr(n, "run_stt") for n in ast.walk(fn)) != 1:
        return False
    if any(_is_self_attr(n, _SEGMENT_PUSH_HELPER) for n in ast.walk(fn)):
        return False
    if _binds_any(fn.body, {audio}):
        return False

    def _is_harmless(stmt: ast.stmt) -> bool:
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Assign):
            return all(isinstance(t, ast.Name) for t in stmt.targets) and isinstance(
                stmt.value, ast.Constant
            )
        if isinstance(stmt, ast.AnnAssign):
            # A local variable's annotation is never evaluated.
            return isinstance(stmt.target, ast.Name) and (
                stmt.value is None or isinstance(stmt.value, ast.Constant)
            )
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = stmt.args
            return (
                not stmt.decorator_list
                and stmt.returns is None
                and not a.defaults
                and not any(a.kw_defaults)
                and all(
                    p.annotation is None
                    for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)
                )
                and (a.vararg is None or a.vararg.annotation is None)
                and (a.kwarg is None or a.kwarg.annotation is None)
            )
        return False

    return all(_is_harmless(stmt) for stmt in _without_docstring(fn.body[: loops[0]]))


class SampleCounter:
    """The one counter used by the feeder and the STT service.

    ``advance`` (samples) / ``add_pcm16_bytes`` return the pipeline sample position of the *start* of
    the chunk just counted. ``reset`` is for ``StartFrame`` only (never flush).
    """

    __slots__ = ("_samples",)

    def __init__(self) -> None:
        self._samples = 0

    @property
    def samples(self) -> int:
        return self._samples

    def advance(self, n_samples: int) -> int:
        start = self._samples
        self._samples += int(n_samples)
        return start

    def add_pcm16_bytes(self, nbytes: int) -> int:
        return self.advance(nbytes // BYTES_PER_SAMPLE)

    def reset(self) -> None:
        self._samples = 0


@dataclass(frozen=True)
class Label:
    speakers: list[str]
    overlap: bool = False


@dataclass
class _Segment:
    start: int
    probs: np.ndarray | None  # (n, slots) float32; None => GAP
    bad: np.ndarray | None = None  # (n,) bool, frames with unmappable activity
    length: int = 0  # samples covered

    @property
    def end(self) -> int:
        return self.start + self.length


class DiarizationTimeline:
    """Session-scoped speaker-probability timeline. See module docstring."""

    def __init__(
        self,
        capacity_sec: float | None = None,
        idle_remint_sec: float | None = None,
    ) -> None:
        cap = RING_CAPACITY_SEC if capacity_sec is None else float(capacity_sec)
        idle = effective_idle_remint_sec(idle_remint_sec)
        self._idle_remint_samples = int(idle * SAMPLE_RATE_HZ)
        self._capacity_samples = int(cap * SAMPLE_RATE_HZ)
        self._lock = threading.RLock()
        self._segments: list[_Segment] = []  # sorted by start
        self._high_water = 0
        self._newest_end = 0
        self._slot_count: int | None = None
        # model slot -> [(first active sample, S{n} number)], sorted by start. A
        # new entry is appended when the slot first speaks and after every idle
        # gap longer than the idle re-mint threshold; numbers are never reused.
        self._gens: dict[int, list[tuple[int, int]]] = {}
        self._last_active: dict[int, int] = {}  # slot -> end of its last active frame
        self._next_label = 1
        self._overflow_warned = False
        self._degraded = False
        self._flushed = False
        self._listeners: list[Listener] = []

    # ------------------------------------------------------------------ writes

    def set_slot_count(self, n: int) -> None:
        with self._lock:
            self._slot_count = int(n)
        self._notify()

    def record(self, start_sample: int, probs) -> None:
        """Worker: record ``probs`` (n, slots) whose row 0 sits at ``start_sample``."""
        arr = np.asarray(probs, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] == 0:
            return
        n = arr.shape[0]
        warn = False
        with self._lock:
            slot_count = self._slot_count
            bad = np.zeros(n, dtype=bool)
            active = arr >= ACTIVITY_THRESHOLD
            # Label slots in order of first activity (earliest frame first).
            for row in np.flatnonzero(active.any(axis=1)):
                pos = int(start_sample) + int(row) * FRAME_SAMPLES
                for slot in np.flatnonzero(active[row]):
                    slot = int(slot)
                    if slot_count is not None and slot >= slot_count:
                        bad[row] = True
                        continue
                    self._touch_slot(slot, pos)
            if bad.any() and not self._overflow_warned:
                self._overflow_warned = True
                warn = True
            self._insert(_Segment(int(start_sample), arr, bad, n * FRAME_SAMPLES))
        if warn:
            logger.warning(
                "DiarizationTimeline: speaker slot beyond model slot count "
                f"({slot_count}); affected windows are left unlabelled"
            )
        self._notify()

    def _touch_slot(self, slot: int, pos: int) -> None:
        """Caller holds ``_lock``. Note activity of ``slot`` at sample ``pos``."""
        gens = self._gens.get(slot)
        if gens is None:
            label = self._take_label()
            self._gens[slot] = [(pos, label)]
            logger.info(
                f"DiarizationTimeline: model slot {slot} first active -> S{label}"
            )
        elif pos - self._last_active[slot] > self._idle_remint_samples:
            idle_sec = (pos - self._last_active[slot]) / SAMPLE_RATE_HZ
            label = self._take_label()
            logger.info(
                f"DiarizationTimeline: model slot {slot} re-minted S{gens[-1][1]} -> "
                f"S{label} after {idle_sec:.0f}s idle"
            )
            gens.append((pos, label))
        self._last_active[slot] = max(
            self._last_active.get(slot, 0), pos + FRAME_SAMPLES
        )

    def _take_label(self) -> int:
        n = self._next_label
        self._next_label += 1
        return n

    def record_gap(self, start: int, end: int) -> None:
        """Worker: mark ``[start, end)`` resolved-but-unlabelled (dropped audio)."""
        if end <= start:
            return
        with self._lock:
            self._insert(_Segment(int(start), None, None, int(end - start)))
        self._notify()

    def mark_flushed(self) -> None:
        with self._lock:
            self._flushed = True
        self._notify()

    def mark_degraded(self, reason: str = "worker") -> None:
        """Idempotent; exactly one WARNING per timeline."""
        with self._lock:
            if self._degraded:
                return
            self._degraded = True
        logger.warning(f"DiarizationTimeline: degraded ({reason}); labels disabled")
        self._notify()

    def add_progress_listener(self, cb: Listener) -> None:
        with self._lock:
            self._listeners.append(cb)

    # ------------------------------------------------------------------- reads

    @property
    def is_ready(self) -> bool:
        """True once the worker has loaded the model and published its slot count."""
        with self._lock:
            return self._slot_count is not None

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    @property
    def is_flushed(self) -> bool:
        return self._flushed

    def coverage_high_water(self) -> int:
        """Highest ``p`` with every position ``< p`` resolved (recorded or GAP)."""
        with self._lock:
            return self._high_water

    def label_for(self, start_sample: int, end_sample: int) -> Label | None:
        """Label ``[start_sample, end_sample)``, or ``None`` if unlabelable.

        A *candidate* is a mapped slot active for ``>= OVERLAP_MIN_FRACTION``
        of the covered rows. Two+ candidates => overlap (top two by
        activity-weighted score). Otherwise the top-scored mapped slot is
        labelled; when it is **not** the sole candidate (no candidate, or the
        candidate scored lower) it must lead the runner-up by
        ``>= DOMINANT_MARGIN`` or the window is left unlabelled. A top slot that
        IS the sole candidate is labelled without a margin check.
        """
        end_sample = int(end_sample)
        start_sample = int(start_sample)
        window = end_sample - start_sample
        if window <= 0:
            return None
        # Snapshot under the lock; compute outside it.
        with self._lock:
            if self._slot_count is None:
                return None
            gens = {s: list(g) for s, g in self._gens.items()}
            parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
            covered = 0
            for seg in self._segments:
                if seg.end <= start_sample:
                    continue
                if seg.start >= end_sample:
                    break
                lo = max(seg.start, start_sample)
                hi = min(seg.end, end_sample)
                if seg.probs is None:
                    continue
                covered += hi - lo
                i0 = max(0, (lo - seg.start) // FRAME_SAMPLES)
                i1 = min(
                    seg.probs.shape[0], -(-(hi - seg.start) // FRAME_SAMPLES)
                )  # ceil
                pos = seg.start + (i0 + np.arange(i1 - i0)) * FRAME_SAMPLES
                parts.append((seg.probs[i0:i1], seg.bad[i0:i1], pos))
        if covered <= 0 or covered / window < MIN_COVERAGE_FRACTION:
            return None
        if any(bad.any() for _, bad, _ in parts):
            return None
        rows = np.concatenate([p for p, _, _ in parts], axis=0)
        positions = np.concatenate([pos for _, _, pos in parts])
        if rows.shape[0] == 0:
            return None
        active = rows >= ACTIVITY_THRESHOLD  # (n, slots)
        frame_active = active.any(axis=1)
        n_active_frames = int(frame_active.sum())
        if n_active_frames == 0:
            return None
        # Activity-weighted: only frames with some active speaker contribute.
        scores = rows[frame_active].mean(axis=0)
        fractions = active.sum(axis=0) / rows.shape[0]
        candidates = [
            s
            for s in range(rows.shape[1])
            if fractions[s] >= OVERLAP_MIN_FRACTION and s in gens
        ]

        def label_number(s: int) -> int:
            # The generation in force at the slot's last active frame in the window.
            last = int(positions[np.flatnonzero(active[:, s])[-1]])
            number = gens[s][0][1]
            for start, n in gens[s]:
                if start > last:
                    break
                number = n
            return number

        def key(s: int) -> tuple[float, int]:
            return (-float(scores[s]), label_number(s))

        if len(candidates) >= 2:
            top2 = sorted(candidates, key=key)[:2]
            return Label([f"S{label_number(s)}" for s in top2], True)
        mapped = [s for s in range(rows.shape[1]) if s in gens and active[:, s].any()]
        if not mapped:
            return None
        ranked = sorted(mapped, key=key)
        top = ranked[0]
        # The margin guard keys on "top is not a candidate", not on "no
        # candidates": a top-scored slot that is not the sole candidate must
        # still clear DOMINANT_MARGIN, or the sole candidate's presence would
        # be overruled with no check at all.
        if top not in candidates and len(ranked) > 1:
            if float(scores[top]) - float(scores[ranked[1]]) < DOMINANT_MARGIN:
                return None
        return Label([f"S{label_number(top)}"], False)

    # ---------------------------------------------------------------- internal

    def _insert(self, seg: _Segment) -> None:
        """Caller holds ``_lock``. Keep sorted, evict, advance high-water."""
        i = len(self._segments)
        while i > 0 and self._segments[i - 1].start > seg.start:
            i -= 1
        self._segments.insert(i, seg)
        self._newest_end = max(self._newest_end, seg.end)
        self._evict()
        self._advance()

    def _evict(self) -> None:
        cutoff = self._newest_end - self._capacity_samples
        if cutoff <= 0:
            return
        keep: list[_Segment] = []
        for seg in self._segments:
            if seg.end <= cutoff:
                continue
            if seg.start < cutoff:
                drop = -(-(cutoff - seg.start) // FRAME_SAMPLES)  # ceil
                if seg.probs is None:
                    seg = _Segment(cutoff, None, None, seg.end - cutoff)
                else:
                    probs = seg.probs[drop:]
                    if probs.shape[0] == 0:
                        continue
                    seg = _Segment(
                        seg.start + drop * FRAME_SAMPLES,
                        probs,
                        seg.bad[drop:],
                        probs.shape[0] * FRAME_SAMPLES,
                    )
            keep.append(seg)
        self._segments = keep
        self._prune_generations(cutoff)
        # Evicted positions are no longer resolvable; never regress, never stall.
        self._high_water = max(self._high_water, cutoff)

    def _prune_generations(self, cutoff: int) -> None:
        """Caller holds ``_lock``. Drop generations wholly before ``cutoff``."""
        for gens in self._gens.values():
            keep_from = 0
            for i, (start, _) in enumerate(gens):
                if start <= cutoff:
                    keep_from = i
            if keep_from:
                del gens[:keep_from]

    def _advance(self) -> None:
        moved = True
        while moved:
            moved = False
            for seg in self._segments:
                if seg.start <= self._high_water < seg.end:
                    self._high_water = seg.end
                    moved = True

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception as exc:  # a listener must never break the worker
                logger.warning(
                    f"DiarizationTimeline: progress listener failed: {exc!r}"
                )
