// Wire-contract v1 encoding + per-stream writer thread.
//
// Handshake: one UTF-8 JSON line {"rate":16000,"width":2,"channels":1,"v":1,
// "nonce":"<echoed>"} + "\n". Frames: 4-byte BIG-endian length prefix + JSON
// object {"seq","captured_monotonic_ns","pcm_b64"}. The PCM inside pcm_b64 is
// 16 kHz PCM16 LE mono; ~640 bytes / 20 ms per frame.

import Foundation

let WIRE_VERSION = 1
let WIRE_RATE = 16000
let WIRE_WIDTH = 2
let WIRE_CHANNELS = 1

/// Serialize + write the one-line handshake. JSONSerialization (not string
/// concat) so an arbitrary nonce value can never produce malformed JSON.
func writeHandshake(fd: Int32, nonce: String?) throws {
    var obj: [String: Any] = [
        "rate": WIRE_RATE,
        "width": WIRE_WIDTH,
        "channels": WIRE_CHANNELS,
        "v": WIRE_VERSION,
    ]
    if let nonce { obj["nonce"] = nonce }
    var line = try JSONSerialization.data(withJSONObject: obj)
    line.append(0x0A)  // "\n"
    switch writeAll(fd: fd, data: line) {
    case .ok: return
    case .peerClosed: throw CapturerError("peer closed during handshake")
    case .failed(let err): throw CapturerError("handshake write failed: \(err)")
    }
}

/// One writer per stream. Capture callbacks enqueue (seq, ns, pcm) without
/// blocking; a dedicated thread encodes and writeAll()s. seq is assigned at
/// capture time, BEFORE the queue, so a local drop-oldest overflow is still
/// observable downstream as a seq gap.
final class FrameWriter {
    let label: String
    private let fd: Int32
    private let onTerminal: (Int32, String) -> Void

    private let lock = NSCondition()
    private var queue: [(seq: UInt64, ns: UInt64, pcm: Data)] = []
    private var closed = false
    private var nextSeq: UInt64 = 0
    private var droppedTotal: UInt64 = 0
    private var thread: Thread?

    /// Local send-queue bound. The recorder applies the real backpressure
    /// policy; this only caps capturer memory if the peer stalls hard
    /// (~256 frames ≈ 5 s of audio). Drop-oldest + WARNING, same spirit.
    private let maxQueuedFrames = 256

    init(label: String, fd: Int32, onTerminal: @escaping (Int32, String) -> Void) {
        self.label = label
        self.fd = fd
        self.onTerminal = onTerminal
    }

    func start() {
        let t = Thread { [weak self] in self?.run() }
        t.name = "writer-\(label)"
        thread = t
        t.start()
    }

    /// Called from audio threads — must not block on the socket.
    func enqueue(pcm: Data, capturedMonotonicNs: UInt64) {
        lock.lock()
        defer { lock.unlock() }
        if closed { return }
        let seq = nextSeq
        nextSeq += 1
        if queue.count >= maxQueuedFrames {
            let dropped = queue.removeFirst()
            droppedTotal += 1
            if droppedTotal == 1 || droppedTotal % 50 == 0 {
                logLine(
                    "WARNING \(label): send queue full (\(maxQueuedFrames)); dropped oldest "
                        + "seq=\(dropped.seq) (total dropped \(droppedTotal))")
            }
        }
        queue.append((seq, capturedMonotonicNs, pcm))
        lock.signal()
    }

    /// Stop accepting frames (teardown path). The fd is closed by the owner.
    func shutdown() {
        lock.lock()
        closed = true
        queue.removeAll()
        lock.signal()
        lock.unlock()
    }

    private func run() {
        while true {
            lock.lock()
            while queue.isEmpty && !closed { lock.wait() }
            if closed && queue.isEmpty {
                lock.unlock()
                return
            }
            let item = queue.removeFirst()
            lock.unlock()

            // base64 emits only [A-Za-z0-9+/=] — safe to embed unescaped.
            let json = "{\"seq\":\(item.seq),\"captured_monotonic_ns\":\(item.ns),"
                + "\"pcm_b64\":\"\(item.pcm.base64EncodedString())\"}"
            let payload = Data(json.utf8)
            var frame = Data(capacity: 4 + payload.count)
            var prefix = UInt32(payload.count).bigEndian
            withUnsafeBytes(of: &prefix) { frame.append(contentsOf: $0) }
            frame.append(payload)

            switch writeAll(fd: fd, data: frame) {
            case .ok:
                continue
            case .peerClosed:
                // One branch closing tears down BOTH (never half-stream).
                // EPIPE is a normal terminal condition (recorder went away).
                onTerminal(ExitCode.ok, "\(label) socket closed by peer")
                shutdown()
                return
            case .failed(let err):
                onTerminal(ExitCode.socketFailed, "\(label) socket write failed: \(err)")
                shutdown()
                return
            }
        }
    }
}
