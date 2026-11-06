import CoreAudio
import SwiftUI

struct SettingsPanelView: View {
    @ObservedObject var model: RecorderModel
    let goBack: () -> Void

    private enum Destination { case settings, diagnostics }
    private enum FocusTarget: Hashable {
        case back, microphone, stt, dataDirectory, config, preview, diagnostics, openLog, logs, quit
    }

    @State private var destination: Destination = .settings
    @FocusState private var focus: FocusTarget?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            if destination == .settings { settings } else { diagnostics }
        }
        .padding(16)
        .frame(width: 360)
        .onAppear { focus = .back }
    }

    private var header: some View {
        HStack {
            Button(action: backAction) { Image(systemName: "chevron.left") }
                .buttonStyle(.plain)
                .focused($focus, equals: .back)
                .accessibilityLabel(destination == .settings ? "Back to recording" : "Back to settings")
                .help(destination == .settings ? "Back to recording controls" : "Back to settings")
            Text(destination == .settings ? "Settings" : "Diagnostics")
                .font(.headline)
            Spacer()
        }
    }

    private var settings: some View {
        VStack(alignment: .leading, spacing: 12) {
            GroupBox("Audio") {
                VStack(alignment: .leading, spacing: 8) {
                    Picker("Microphone (Me)", selection: micSelection) {
                        ForEach(model.inputDevices) { device in
                            Text(device.name).tag(device.id)
                        }
                    }
                    .focused($focus, equals: .microphone)
                    Text("System output (Them): \(model.outputDevice)")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Microphone changes update the macOS system default immediately")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GroupBox("Speech to text") {
                VStack(alignment: .leading, spacing: 8) {
                    Picker("Configured service", selection: sttSelection) {
                        ForEach(RecorderModel.sttServices, id: \.self) { service in
                            Text(service).tag(service)
                        }
                    }
                    .focused($focus, equals: .stt)
                    Button("Open config.toml for endpoint settings") { model.openConfig() }
                        .focused($focus, equals: .config)
                    appliesNextStart
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GroupBox("Storage and privacy") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Data directory: \(model.nextDataDirDisplay)")
                        .font(.caption).textSelection(.enabled)
                    Button("Change Data Directory…") { model.chooseDataDir() }
                        .focused($focus, equals: .dataDirectory)
                    appliesNextStart
                    Toggle(
                        "Show latest transcript preview",
                        isOn: Binding(
                            get: { model.transcriptPreviewEnabled },
                            set: { model.setTranscriptPreviewEnabled($0) })
                    )
                    .focused($focus, equals: .preview)
                    .accessibilityHint("Off by default; affects display only")
                    Text("Transcript preview visibility updates immediately")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Button("Diagnostics…") { destination = .diagnostics; focus = .openLog }
                .focused($focus, equals: .diagnostics)

            Divider()
            Button(quitLabel) { model.quitApp() }
                .focused($focus, equals: .quit)
                .keyboardShortcut("q")
                .help(quitLabel)
        }
    }

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: 12) {
            GroupBox("Logs") {
                VStack(alignment: .leading, spacing: 8) {
                    if let started = model.mostRecentRunStartedAt {
                        Text("Most recent app-owned run: \(started.formatted(date: .abbreviated, time: .standard))")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("No app-owned run in this launch")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Button("Open Log") { model.openLog() }
                        .focused($focus, equals: .openLog)
                    Button("Show Logs Folder") { model.showLogsFolder() }
                        .focused($focus, equals: .logs)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GroupBox("Command line") {
                VStack(alignment: .leading, spacing: 5) {
                    LabeledContent("Path", value: model.cliPath)
                    LabeledContent("Status", value: model.cliAvailable ? "Available" : "Missing or not executable")
                    if !model.cliAvailable {
                        Text("Install the CLI with `make -C native install-cli`, or correct the cliPath preference.")
                            .font(.caption).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .font(.caption)
                .textSelection(.enabled)
            }

            GroupBox("Directories") {
                VStack(alignment: .leading, spacing: 5) {
                    if let active = model.activeDataDirDisplay {
                        LabeledContent("Active", value: active)
                    } else {
                        LabeledContent("Active", value: "None")
                    }
                    LabeledContent("Next recording", value: model.nextDataDirDisplay)
                }
                .font(.caption)
                .textSelection(.enabled)
            }

            Spacer(minLength: 0)
        }
    }

    private var appliesNextStart: some View {
        Text("Changes apply on next Start")
            .font(.caption2).foregroundStyle(.secondary)
    }

    private func backAction() {
        if destination == .diagnostics {
            destination = .settings
            focus = .diagnostics
        } else {
            goBack()
        }
    }

    private var quitLabel: String {
        if case .running(ours: true) = model.state { return "Stop & Quit Onoats" }
        return "Quit Onoats"
    }

    private var micSelection: Binding<AudioObjectID> {
        Binding(
            get: { model.defaultInputID },
            set: { id in
                if let device = model.inputDevices.first(where: { $0.id == id }) {
                    model.setMicDevice(device)
                }
            })
    }

    private var sttSelection: Binding<String> {
        Binding(get: { model.sttService }, set: { model.setSTTService($0) })
    }
}
