// Unix-domain SOCK_STREAM server side of the wire contract: the capturer
// bind()+listen()s and CREATES both socket files (this is what unblocks the
// supervisor's bounded _wait_for_sockets); the recorder is the client.

import Foundation

/// Create a listening unix socket at `path`. Unlinks a pre-existing file first
/// (the supervisor mints a fresh private 0700 dir per generation, so a
/// collision is residue, never a live peer).
func makeListeningSocket(path: String) throws -> Int32 {
    var addr = sockaddr_un()
    let maxLen = MemoryLayout.size(ofValue: addr.sun_path)  // 104 on macOS
    let pathBytes = Array(path.utf8CString)  // includes trailing NUL
    guard pathBytes.count <= maxLen else {
        throw CapturerError("socket path too long (\(path.utf8.count) > \(maxLen - 1) bytes): \(path)")
    }
    unlink(path)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else {
        throw CapturerError("socket(): \(String(cString: strerror(errno)))")
    }
    addr.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &addr.sun_path) { dst in
        pathBytes.withUnsafeBytes { src in
            dst.copyMemory(from: UnsafeRawBufferPointer(rebasing: src[0..<pathBytes.count]))
        }
    }
    let bindResult = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard bindResult == 0 else {
        let err = String(cString: strerror(errno))
        close(fd)
        throw CapturerError("bind(\(path)): \(err)")
    }
    guard listen(fd, 1) == 0 else {
        let err = String(cString: strerror(errno))
        close(fd)
        throw CapturerError("listen(\(path)): \(err)")
    }
    return fd
}

/// Accept exactly one connection on EACH listening fd within one shared
/// deadline. This is the startup barrier: socket-file existence is not proof
/// the recorder connected, so we block here until BOTH branches have a peer —
/// and if either misses the deadline we throw, failing BOTH branches loud.
///
/// `onAccept` runs per connection AS IT ARRIVES (not after the barrier): the
/// recorder bounds its post-connect handshake read at `read_idle_timeout`
/// (10 s), which is shorter than this accept deadline (30 s) — handshaking
/// only after both accepts would let a healthy-but-staggered startup time out
/// the early branch.
func acceptBoth(
    listenFds: [(label: String, fd: Int32)], deadlineSeconds: Double,
    onAccept: (Int32) throws -> Void = { _ in }
) throws -> [Int32] {
    let deadlineNs = MonotonicClock.nowNanos() + UInt64(deadlineSeconds * 1e9)
    var accepted: [Int32: Int32] = [:]  // listen fd -> connection fd

    while accepted.count < listenFds.count {
        let nowNs = MonotonicClock.nowNanos()
        guard nowNs < deadlineNs else {
            throw CapturerError(
                "startup deadline (\(deadlineSeconds)s) expired before both branches "
                    + "connected (\(accepted.count)/\(listenFds.count) accepted) — failing both loud")
        }
        var pollFds = listenFds
            .filter { accepted[$0.fd] == nil }
            .map { pollfd(fd: $0.fd, events: Int16(POLLIN), revents: 0) }
        let timeoutMs = Int32(min((deadlineNs - nowNs) / 1_000_000, 1000))
        let n = poll(&pollFds, nfds_t(pollFds.count), timeoutMs)
        if n < 0 {
            if errno == EINTR { continue }
            throw CapturerError("poll(): \(String(cString: strerror(errno)))")
        }
        for p in pollFds where (p.revents & Int16(POLLIN)) != 0 {
            let conn = accept(p.fd, nil, nil)
            guard conn >= 0 else {
                if errno == EINTR || errno == ECONNABORTED { continue }
                throw CapturerError("accept(): \(String(cString: strerror(errno)))")
            }
            // Belt and suspenders alongside the global SIG_IGN: a peer close
            // must surface as EPIPE from write(), never a SIGPIPE kill.
            var one: Int32 = 1
            setsockopt(conn, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
            accepted[p.fd] = conn
            if let label = listenFds.first(where: { $0.fd == p.fd })?.label {
                logLine("accepted \(label) connection")
            }
            try onAccept(conn)
        }
    }
    return listenFds.map { accepted[$0.fd]! }
}

enum WriteOutcome {
    case ok
    case peerClosed  // EPIPE/ECONNRESET — normal terminal condition, not a crash
    case failed(String)
}

/// A SOCK_STREAM write can be partial: loop until the whole buffer is on the
/// wire or the write terminally fails. Never returns a short write.
func writeAll(fd: Int32, data: Data) -> WriteOutcome {
    data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> WriteOutcome in
        guard let base = raw.baseAddress else { return .ok }
        var offset = 0
        while offset < raw.count {
            let n = write(fd, base + offset, raw.count - offset)
            if n > 0 {
                offset += n
                continue
            }
            if n < 0 && errno == EINTR { continue }
            if n < 0 && (errno == EPIPE || errno == ECONNRESET) { return .peerClosed }
            return .failed(String(cString: strerror(errno)))
        }
        return .ok
    }
}
