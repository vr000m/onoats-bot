import AppKit
import SwiftUI

/// The primary, fixed-width recording surface. RecorderModel remains the sole
/// owner of lifecycle routing; this view only presents its authoritative state.
struct RecordingPanelView: View {
    @ObservedObject var model: RecorderModel
    @ObservedObject var launch: LaunchState
    let showSettings: () -> Void

    private enum FocusTarget: Hashable {
        case seminar, primary, flush, settings
    }

    @FocusState private var focus: FocusTarget?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            statusSection
            warningSection
            directoryDisclosure
            Divider()
            controls
        }
        .padding(16)
        .frame(width: 360)
        .onAppear { focus = initialFocus }
    }

    private var header: some View {
        HStack(spacing: 10) {
            OatsMarkView()
                .frame(width: 28, height: 28)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 0) {
                Text("Oats").font(.headline)
                Text("Organized Transcript System")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Oats - Organized Transcript System")
            Spacer()
            Button(action: showSettings) {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.plain)
            .focused($focus, equals: .settings)
            .accessibilityLabel("Settings")
            .help("Open Onoats settings and diagnostics")
        }
    }

    @ViewBuilder
    private var statusSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Circle().fill(statusColour).frame(width: 9, height: 9)
                Text(statusTitle).font(.headline)
                Spacer()
                if let start = model.startTime, isRunning {
                    Text(start, style: .timer)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("Recording elapsed time")
                }
            }

            if case .running(ours: false) = model.state {
                Text("Recording started outside the menu bar")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if case .failed(let reason, let detail) = model.state {
                Text(model.seminarConfigurationFailure
                    ? "Seminar needs 'seminars' in config.toml [categories]"
                    : "Last session failed: \(reason)")
                    .font(.callout.weight(.medium))
                if !model.seminarConfigurationFailure, let detail, !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
            }

            if isRunning {
                categoryBadge
                activityRows
                sttRow
                transcriptRow
            }
        }
    }

    @ViewBuilder
    private var categoryBadge: some View {
        if model.categoryForDisplay == RecorderModel.seminarCategory {
            Label("Seminar", systemImage: "lock.fill")
                .font(.caption.weight(.semibold))
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(.quaternary, in: Capsule())
                .accessibilityLabel("Seminar category locked")
        } else if model.categoryUnavailable {
            Label("Category unavailable", systemImage: "questionmark.circle")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var activityRows: some View {
        VStack(spacing: 6) {
            activityRow("Me", activity: model.meActivity)
            activityRow("Them", activity: model.themActivity)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Audio activity")
    }

    private func activityRow(_ label: String, activity: UiTelemetryActivity?) -> some View {
        HStack(spacing: 8) {
            Text(label).frame(width: 38, alignment: .leading)
            if let activity, model.telemetryAvailable {
                ProgressView(value: activity.level, total: 1)
                    .progressViewStyle(.linear)
                Text(activity.level > 0 ? "Signal" : "Silent")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(width: 76, alignment: .trailing)
            } else {
                ProgressView(value: 0, total: 1)
                    .progressViewStyle(.linear).opacity(0.45)
                Text(model.telemetryAvailable ? "No signal" : "Unavailable")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(width: 76, alignment: .trailing)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var sttRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("STT").frame(width: 38, alignment: .leading)
            Circle().fill(sttColour).frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(sttSummary).font(.callout)
                if let aggregate = model.aggregateStt, aggregate != .ready {
                    if let me = model.meStt { sttDetail("Me", me) }
                    if let them = model.themStt { sttDetail("Them", them) }
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func sttDetail(_ label: String, _ branch: UiTelemetrySttBranch) -> some View {
        Text("\(label): \(branch.state.rawValue)\(branch.detail.map { " — \($0)" } ?? "")")
            .font(.caption).foregroundStyle(.secondary)
            .lineLimit(2)
    }

    @ViewBuilder
    private var transcriptRow: some View {
        if model.transcriptPreviewEnabled, let preview = model.transcriptPreview {
            let label = transcriptDisplayLabel(
                source: preview.source, speakerLabel: preview.speakerLabel)
            Text("\(label): \(preview.text)")
                .font(.caption)
                .lineLimit(1)
                .truncationMode(.tail)
                .accessibilityLabel("Latest transcript preview")
        }
    }

    @ViewBuilder
    private var warningSection: some View {
        let warnings = warningMessages
        if !warnings.isEmpty {
            VStack(alignment: .leading, spacing: 7) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(warnings.enumerated()), id: \.offset) { _, warning in
                            ForEach(Array(hintCaptionLines(warning).enumerated()), id: \.offset) { i, line in
                                Text(i == 0 ? "⚠ \(line)" : "   \(line)")
                                    .font(.caption)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                .frame(maxHeight: 104)

                HStack {
                    if model.canOpenLogForCurrentSurface {
                        Button("Open Log") { model.openLog() }
                    } else if case .running(ours: false) = model.state {
                        Text("Log not captured (started outside the menu bar)")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if model.seminarConfigurationFailure {
                        Button("Open Config") { model.openConfig() }
                    }
                }
            }
            .padding(9)
            .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            .accessibilityElement(children: .contain)
        }
    }

    @ViewBuilder
    private var directoryDisclosure: some View {
        if let active = model.activeDataDirDisplay,
           active != model.nextDataDirDisplay {
            VStack(alignment: .leading, spacing: 2) {
                Text("Recording to: \(active)")
                Text("Next recording: \(model.nextDataDirDisplay)")
            }
            .font(.caption).foregroundStyle(.secondary)
            .textSelection(.enabled)
        }
    }

    @ViewBuilder
    private var controls: some View {
        switch model.state {
        case .stopped, .failed:
            HStack(spacing: 10) {
                Image(systemName: "person.3.fill")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Seminar").font(.callout.weight(.semibold))
                    Text("Applies to the next recording")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle(isOn: $model.seminarMode) { EmptyView() }
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .focused($focus, equals: .seminar)
                    .accessibilityLabel("Seminar")
                    .accessibilityHint("Applies to the next recording only")
            }
            .padding(10)
            .background(.quaternary.opacity(0.7), in: RoundedRectangle(cornerRadius: 10))
            Button("Start Recording") { model.start() }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .frame(maxWidth: .infinity)
                .focused($focus, equals: .primary)
                .keyboardShortcut(.defaultAction)
                .disabled(!model.cliAvailable)
                .help(model.cliAvailable ? "Start recording" : "CLI unavailable; see Settings, Diagnostics")
        case .running(let ours):
            Button("Stop Recording") {
                if ours { model.stop() } else { model.stopExternal() }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .frame(maxWidth: .infinity)
            .focused($focus, equals: .primary)
            .keyboardShortcut(.defaultAction)
            .disabled(model.stopRequested)
            .help("Stop recording and drain pending transcripts")
            Button("Flush") { model.flush() }
                .focused($focus, equals: .flush)
                .help("Rotate the current transcript and keep recording")
        case .starting:
            Button("Starting…") {}
                .buttonStyle(.borderedProminent).controlSize(.large)
                .frame(maxWidth: .infinity).disabled(true)
        case .stopping:
            Button("Stopping and draining…") {}
                .buttonStyle(.borderedProminent).controlSize(.large)
                .frame(maxWidth: .infinity).disabled(true)
        }
    }

    private var initialFocus: FocusTarget {
        switch model.state {
        case .stopped, .failed: return .seminar
        case .running: return .primary
        case .starting, .stopping: return .settings
        }
    }

    private var isRunning: Bool {
        if case .running = model.state { return true }
        return false
    }

    private var warningMessages: [String] {
        var values: [String] = []
        if let warning = model.warning, !warning.isEmpty { values.append(warning) }
        if let detail = model.prestartDetail, !detail.isEmpty { values.append(detail) }
        if model.schemaDrift { values.append("Status file schema drift — update onoats / this app") }
        if let note = model.flushNote, !note.isEmpty { values.append(note) }
        if let hint = launch.loginItemHint, !hint.isEmpty { values.append(hint) }
        if model.seminarConfigurationFailure {
            values.append("Seminar configuration needs attention")
        } else if case .failed = model.state, model.canOpenLogForCurrentSurface {
            values.append("The app-owned recorder exited unexpectedly")
        }
        return values
    }

    private var statusTitle: String {
        switch model.state {
        case .stopped: return model.cliAvailable ? "Ready to record" : "Recorder unavailable"
        case .starting: return "Starting…"
        case .running: return model.stopRequested ? "Stopping (draining)…" : "Recording"
        case .stopping: return "Stopping (draining)…"
        case .failed: return "Recording failed"
        }
    }

    private var statusColour: Color {
        switch model.state {
        case .running: return model.warning == nil ? .red : .orange
        case .starting, .stopping: return .orange
        case .failed: return .red
        case .stopped: return model.cliAvailable ? .secondary : .orange
        }
    }

    private var sttSummary: String {
        guard let state = model.aggregateStt else {
            return model.sttLabel.map { "\($0) — status unavailable" } ?? "Status unavailable"
        }
        if state == .unknown {
            return "status not reported (\(model.sttLabel ?? model.sttService))"
        }
        return state.rawValue.capitalized
    }

    private var sttColour: Color {
        switch model.aggregateStt {
        case .ready: return .green
        case .failed: return .red
        case .recovering, .starting: return .orange
        case .unknown, .none: return .secondary
        }
    }
}

/// Crops the standalone grain-wave mark out of the canonical header export so
/// transparent wordmark bounds never participate in panel layout.
private struct OatsMarkView: View {
    private static let mark: NSImage? = {
        guard let source = NSImage(named: NSImage.Name("OnoatsHeader")),
              let image = source.cgImage(forProposedRect: nil, context: nil, hints: nil)
        else { return nil }
        let side = image.height
        guard let crop = image.cropping(to: CGRect(x: 0, y: 0, width: side, height: side))
        else { return nil }
        return NSImage(
            cgImage: crop, size: NSSize(width: CGFloat(side), height: CGFloat(side)))
    }()

    @ViewBuilder
    var body: some View {
        if let mark = Self.mark {
            Image(nsImage: mark)
                .resizable()
                .scaledToFit()
        } else {
            Image(systemName: "waveform")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Color.accentColor)
        }
    }
}
