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
``SLOT_IDLE_REMINT_SEC`` is given a fresh number on its next active frame, so a
label does not silently move to a different speaker if the model recycles the
slot. A slot therefore has a list of generations ``(start_sample, n)``.
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
#: cannot catch a swap while both speakers are active.
SLOT_IDLE_REMINT_SEC = 300.0
IDLE_REMINT_SAMPLES = int(SLOT_IDLE_REMINT_SEC * SAMPLE_RATE_HZ)
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
    ``run_stt`` inline in the stop handler; from 1.9.0 the handler puts the
    segment on ``_segment_queue`` and ONE ``_segment_task_handler`` task takes
    them off one at a time. The queue shape is only in order if the whole
    class agrees (:func:`_segment_queue_is_one_fifo_consumer`): the queue is
    built once as a plain ``asyncio.Queue()`` (not LIFO / priority),
    ``start`` creates exactly one handler task, the queue's only reader, each
    VAD-stop puts exactly one segment, and that reader is one loop that
    awaits ``run_stt`` once per segment until the ``None`` sentinel. Anything
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
        return src.count("self.run_stt(") == 1 and "queue" not in src.lower()
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
    requires, exactly as pipecat 1.9-1.12 write it:

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
    - the reader drains one-for-one (:func:`_reader_drains_one_for_one`).

    Fails closed on an unreadable/unparseable method or a ``"_segment_queue"``
    string (a ``setattr`` the walk cannot follow).
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
            if isinstance(node, ast.Constant) and node.value == "_segment_queue":
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
    if not _reader_drains_one_for_one(methods["_segment_task_handler"]):
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


def _stop_handler_puts_one_segment(tree: object) -> bool:
    """True iff the VAD-stop handler puts its segment on EVERY stop it does not drop.

    The STT appends one span per VAD-stop and skips only when ``is_usable`` is
    false (mirroring pipecat 1.9-1.12, which drops that segment). So the put
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

    def _is_usable_guard(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.UnaryOp)
            and isinstance(stmt.test.op, ast.Not)
            and _is_self_attr(stmt.test.operand, "is_usable")
            and not stmt.orelse
        )

    if sum(_is_put_stmt(stmt) for stmt in fn.body) != 1:
        return False
    for stmt in fn.body:
        if _is_usable_guard(stmt):
            continue
        if any(isinstance(n, (ast.Return, ast.Raise)) for n in ast.walk(stmt)):
            return False
    return True


def _reader_drains_one_for_one(tree: object) -> bool:
    """True iff ``_segment_task_handler`` awaits ``run_stt`` once per queued segment.

    Requires pipecat 1.9-1.12's exact reader shape::

        running = True
        while running:
            audio = await self._segment_queue.get()
            running = audio is not None
            if audio:
                try:
                    await self.process_generator(self.run_stt(audio))
                except Exception as e: ...

    i.e. one top-level ``while <flag>:`` loop (no ``else``) ended ONLY by the
    ``None`` sentinel (``<flag>`` is otherwise only ever ``True``), with no
    ``return`` / ``break`` / ``raise`` inside it, whose body gets one item and
    awaits the handler's ONE ``run_stt`` call on it - reached through nothing
    but ``if <item>:`` and a ``try`` body. A one-shot reader, a ``run_stt``
    outside the loop, a second or skippable ``run_stt`` would strand a span
    or pair it with another segment.

    ``if <item>:`` skips an empty segment without calling ``run_stt``. That
    is unreachable for ``WebSocketSTTService``: it keeps pipecat's default
    ``wants_wav_segments`` (True), so every segment is a non-empty WAV.
    """
    import ast

    fn = _function_def(tree)
    if fn is None:
        return False
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    if len(loops) != 1 or loops[0] not in fn.body:
        return False
    loop = loops[0]
    if loop.orelse or not isinstance(loop.test, ast.Name):
        return False
    flag = loop.test.id
    if any(isinstance(n, (ast.Return, ast.Break, ast.Raise)) for n in ast.walk(loop)):
        return False

    # ``<item> = await self._segment_queue.get()``, directly in the loop body.
    gets = [
        stmt
        for stmt in loop.body
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Await)
        and _is_queue_call(stmt.value.value, "get")
        and not stmt.value.value.args
        and not stmt.value.value.keywords
    ]
    if len(gets) != 1:
        return False
    item = gets[0].targets[0].id

    def _is_sentinel_test(value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Compare)
            and isinstance(value.left, ast.Name)
            and value.left.id == item
            and len(value.ops) == 1
            and isinstance(value.ops[0], ast.IsNot)
            and isinstance(value.comparators[0], ast.Constant)
            and value.comparators[0].value is None
        )

    # Every write of the loop flag is ``True`` outside the loop or the one
    # ``<flag> = <item> is not None`` directly in its body.
    sentinel_writes = 0
    for node in ast.walk(fn):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            targets = [getattr(node, "target", None)] + [
                item_.optional_vars for item_ in getattr(node, "items", [])
            ]
        for target in targets:
            if target is None:
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == flag for t in ast.walk(target)
            ):
                continue
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                return False
            if node in loop.body and _is_sentinel_test(node.value):
                sentinel_writes += 1
            elif not (
                node in fn.body
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
            ):
                return False
    if sentinel_writes != 1:
        return False

    # The handler's one ``run_stt`` call: ``await self.process_generator(
    # self.run_stt(<item>))``, inside the loop under only ``if <item>:``/``try``.
    calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _is_self_attr(n.func, "run_stt")
    ]
    if len(calls) != 1:
        return False
    run_stt = calls[0]
    if not (
        len(run_stt.args) == 1
        and not run_stt.keywords
        and isinstance(run_stt.args[0], ast.Name)
        and run_stt.args[0].id == item
    ):
        return False

    def _reaches(stmts: list[ast.stmt]) -> bool:
        for stmt in stmts:
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Await)
                and isinstance(stmt.value.value, ast.Call)
                and _is_self_attr(stmt.value.value.func, "process_generator")
                and stmt.value.value.args == [run_stt]
                and not stmt.value.value.keywords
            ):
                return True
            if (
                isinstance(stmt, ast.If)
                and isinstance(stmt.test, ast.Name)
                and stmt.test.id == item
                and not stmt.orelse
                and _reaches(stmt.body)
            ):
                return True
            if isinstance(stmt, ast.Try) and not stmt.orelse and _reaches(stmt.body):
                return True
        return False

    return _reaches(loop.body)


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

    def __init__(self, capacity_sec: float | None = None) -> None:
        cap = RING_CAPACITY_SEC if capacity_sec is None else float(capacity_sec)
        self._capacity_samples = int(cap * SAMPLE_RATE_HZ)
        self._lock = threading.RLock()
        self._segments: list[_Segment] = []  # sorted by start
        self._high_water = 0
        self._newest_end = 0
        self._slot_count: int | None = None
        # model slot -> [(first active sample, S{n} number)], sorted by start. A
        # new entry is appended when the slot first speaks and after every idle
        # gap longer than SLOT_IDLE_REMINT_SEC; numbers are never reused.
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
            self._gens[slot] = [(pos, self._take_label())]
        elif pos - self._last_active[slot] > IDLE_REMINT_SAMPLES:
            gens.append((pos, self._take_label()))
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
