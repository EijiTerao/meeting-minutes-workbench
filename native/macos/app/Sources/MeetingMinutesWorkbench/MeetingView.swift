import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct MeetingView: View {
    @ObservedObject var workspace: WorkbenchWorkspace
    var body: some View {
        // Structure and geometry transplanted from the private MeetingView.swift.
        VStack(spacing: 0) {
            MeetingWorkspaceTabs(workspace: workspace)
            ZStack(alignment: .topLeading) {
                MeetingPrimaryWorkspacePage(model: workspace, isSelected: workspace.selectedID == nil)
                    .opacity(workspace.selectedID == nil ? 1 : 0)
                    .allowsHitTesting(workspace.selectedID == nil)
                    .accessibilityHidden(workspace.selectedID != nil)
                ForEach(workspace.savedTabs) { tab in
                    MeetingPrimaryWorkspacePage(model: workspace, recordID: tab.id, isSelected: workspace.selectedID == tab.id)
                        .opacity(workspace.selectedID == tab.id ? 1 : 0)
                        .allowsHitTesting(workspace.selectedID == tab.id)
                        .accessibilityHidden(workspace.selectedID != tab.id)
                }
            }
        }
        .frame(minWidth: 980, idealWidth: 1000, minHeight: 680, idealHeight: 720)
        .meetingInteractionDefaults()
    }
}

private struct MeetingPrimaryWorkspacePage: View {
    @ObservedObject var model: WorkbenchWorkspace
    var recordID: UUID? = nil
    var isSelected: Bool
    var body: some View {
        VStack(spacing: 0) {
            MeetingHeader(model: model, recordID: recordID)
            // The original playback bar requires the private MeetingAudioPlaybackModel.
            // Public raw recordings are not yet wired to an in-app playback controller.
            HSplitView {
                MeetingNotesPane(model: model, recordID: recordID).frame(minWidth: 390)
                MeetingSupportPane(model: model, recordID: recordID).frame(minWidth: 430)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// View structure/styles derived from MeetingTabWorkspace.swift and MeetingComponents.swift.
private struct MeetingWorkspaceTabs: View {
    @ObservedObject var workspace: WorkbenchWorkspace
    var body: some View {
        HStack(spacing: 8) {
            MeetingWorkspaceTabButton(id: nil, workspace: workspace)
            if !workspace.savedTabs.isEmpty {
                Text("履歴").font(.caption).foregroundStyle(.secondary).accessibilityAddTraits(.isHeader)
            }
            ScrollView(.horizontal) {
                HStack(spacing: 6) {
                    ForEach(workspace.savedTabs) { tab in
                        HStack(spacing: 2) {
                            MeetingWorkspaceTabButton(id: tab.id, workspace: workspace)
                            Button { workspace.closeHistory(tab.id) } label: { Image(systemName: "xmark").frame(width: 20, height: 20) }
                                .buttonStyle(.plain).accessibilityLabel("\(tab.title)のタブを閉じる")
                        }.id(tab.id)
                    }
                }
            }.frame(minWidth: 0)
            Button { workspace.presentHistoryPicker() } label: { Image(systemName: "plus") }
                .buttonStyle(.bordered).accessibilityLabel("履歴を別タブで開く")
                .popover(isPresented: $workspace.historyPickerPresented, arrowEdge: .bottom) { MeetingHistoryTabPicker(workspace: workspace) }
        }
        .padding(.horizontal, 8).padding(.vertical, 5).background(.bar).frame(height: 42).fixedSize(horizontal: false, vertical: true)
    }
}

private struct MeetingWorkspaceTabButton: View {
    let id: UUID?
    @ObservedObject var workspace: WorkbenchWorkspace
    private var selected: Bool { workspace.selectedID == id }
    private var record: WorkbenchRecord { id.flatMap { key in workspace.savedTabs.first { $0.id == key } } ?? workspace.primary }
    var body: some View {
        Button { workspace.select(id) } label: {
            HStack(spacing: 5) {
                if record.isRecording { Text("●").foregroundStyle(.red) }
                else if record.isBusy { Text("…").foregroundStyle(.orange) }
                Text(id == nil ? (selected ? "作業タブ" : "作業タブに戻る") : record.title).lineLimit(1)
                if id == nil { Text(record.title).lineLimit(1).frame(maxWidth: 132, alignment: .leading) }
            }.padding(.horizontal, 8).padding(.vertical, 5).frame(minHeight: 30)
        }
        .buttonStyle(.bordered).tint(selected ? .accentColor : .secondary)
        .overlay(alignment: .bottom) { if selected { Capsule().fill(Color.accentColor).frame(height: 2).padding(.horizontal, 7).padding(.bottom, 2) } }
        .accessibilityAddTraits(selected ? .isSelected : []).help(record.title)
        .accessibilityLabel("\(id == nil ? (selected ? "作業タブ" : "作業タブに戻る") : "保存済み会議")、\(record.title)\(record.isRecording ? "、録音中" : record.isBusy ? "、処理中" : "")")
        .accessibilityIdentifier(id.map { "meeting-tab-\($0.uuidString)" } ?? "meeting-tab-live")
    }
}

private struct MeetingHistoryTabPicker: View {
    @ObservedObject var workspace: WorkbenchWorkspace
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("履歴を別タブで開く").font(.headline)
            let entries = workspace.loadHistory()
            if entries.isEmpty { Text("保存済み会議はありません").font(.caption).foregroundStyle(.secondary) }
            else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        let groups = Dictionary(grouping: entries) { $0.topic.isEmpty ? "分類なし" : $0.topic }
                        ForEach(groups.keys.sorted(), id: \.self) { topic in
                            Text(topic).font(.caption.bold()).foregroundStyle(.secondary).padding(.top, 5)
                            ForEach(groups[topic] ?? []) { entry in
                            Button { workspace.addHistoryTab(entry.id) } label: {
                                HStack { VStack(alignment: .leading, spacing: 2) { Text(entry.title).lineLimit(1); Text("原文 \(entry.transcript.count)文字").font(.caption).foregroundStyle(.secondary) }; Spacer(minLength: 0) }
                                    .padding(.horizontal, 10).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                                    .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 6))
                            }.buttonStyle(.plain)
                            }
                        }
                    }
                }
            }
        }.padding(12).frame(width: 360, height: 420)
    }
}

private struct MeetingHeader: View {
    @ObservedObject var model: WorkbenchWorkspace
    let recordID: UUID?
    @State private var showCaptureSettings = false
    @State private var showOnlineDetails = false
    @State private var showSafetyNotice = false
    private var record: WorkbenchRecord { recordID.flatMap { id in model.savedTabs.first { $0.id == id } } ?? model.primary }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) { Text("会議メモ").font(.title2.bold()); Text("録音を停止 → 原文を確認 → 議事録を作成").font(.caption).foregroundStyle(.secondary) }
                Spacer()
                Menu("取り込む") {
                    Button("文字起こしTXTを開く…") { importTranscript() }
                        .disabled(record.isRecording || record.isBusy)
                }
                Menu("共有用に書き出す") {
                    Button("本文…") { exportText(record.minutes, name: "meeting-minutes.txt") }.disabled(record.minutes.isEmpty)
                    Button("未決事項…") { exportText(record.openItems, name: "open-items.txt") }.disabled(record.openItems.isEmpty)
                }.disabled(record.minutes.isEmpty && record.openItems.isEmpty)
                Button("新しい会議") { model.createNewMeeting() }.disabled(record.isRecording || record.isBusy || recordID != nil)
                Button("履歴") { model.showHistory = true }.sheet(isPresented: $model.showHistory) { MeetingHistorySheet(model: model) }
            }
            HStack(spacing: 12) {
                TextField("会議名", text: binding(\.title)).textFieldStyle(.roundedBorder).frame(minWidth: 220)
                    .onSubmit { model.saveTitle(recordID: recordID) }.disabled(record.isRecording || record.isBusy)
                Button("保存") { model.saveTitle(recordID: recordID) }.disabled(record.isRecording || record.isBusy)
                Picker("形式", selection: $model.mode) { ForEach(WorkbenchWorkspace.CaptureMode.allCases) { Text($0.rawValue).tag($0) } }
                    .pickerStyle(.segmented).frame(width: 180).meetingActionCursor()
                    .disabled(model.hasActiveCapture || recordID != nil)
                Spacer()
            }
            DisclosureGroup(model.retainRawAudio ? "収音設定 · 原音も保存" : "収音設定 · 文字起こしのみ", isExpanded: $showCaptureSettings) {
                VStack(alignment: .leading, spacing: 8) {
                    if model.mode == .online {
                        Toggle("相手の音声のみ（マイクOFF）", isOn: $model.remoteAudioOnly)
                            .toggleStyle(MeetingFormCheckboxStyle())
                            .help("チェックするとマイクを使わず、選んだ再生音だけを取り込みます。")
                        DisclosureGroup("収音対象（指定は任意）", isExpanded: $showOnlineDetails) {
                            let options = runningCaptureApplications()
                            Picker("会議アプリ", selection: $model.applicationBundleID) {
                                Text("指定なし（Mac全体）").tag(nil as String?)
                                if let selected = model.applicationBundleID, !options.contains(where: { $0.id == selected }) {
                                    Text("選んだアプリ（現在は未起動）").tag(Optional(selected))
                                }
                                ForEach(options) { Text($0.label).tag(Optional($0.id)) }
                            }.frame(maxWidth: 300, alignment: .leading).meetingActionCursor()
                            Text("ブラウザを選んだ場合、同じブラウザの他のタブの音も入り得ます。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Toggle("原音を保存する", isOn: $model.retainRawAudio)
                        .toggleStyle(MeetingFormCheckboxStyle())
                    Text(model.retainRawAudio ? "文字起こしと原音を保存します。" : "文字起こしのみ保存します。原音ファイルは作りません。")
                        .font(.caption).foregroundStyle(.secondary)
                }.disabled(model.hasActiveCapture || recordID != nil)
            }
            HStack {
                if record.isRecording {
                    Button("録音を停止") { model.stopRecording() }
                        .buttonStyle(MeetingActionButtonStyle(kind: .primary)).controlSize(.large)
                        .accessibilityIdentifier("meeting-stop-capture")
                } else {
                    Button("録音を開始") { showSafetyNotice = true }
                        .disabled(model.hasActiveCapture || recordID != nil)
                        .buttonStyle(MeetingActionButtonStyle(kind: .primary)).controlSize(.large)
                        .accessibilityIdentifier("meeting-start-capture")
                        .sheet(isPresented: $showSafetyNotice) {
                            CaptureConfirmation(scope: model.captureScope, saveRaw: model.retainRawAudio) {
                                showSafetyNotice = false
                                model.confirmStartRecording()
                            } cancel: { showSafetyNotice = false }
                        }
                }
                Text(model.captureScope).font(.caption).foregroundStyle(.secondary)
                if model.generationLeaseID == record.id { Text("議事録を作成中…").font(.caption).foregroundStyle(.orange) }
                Spacer()
            }
            if !model.notice.isEmpty { Text(model.notice).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
        }
        .padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 10)
    }
    private func binding<T>(_ keyPath: WritableKeyPath<WorkbenchRecord, T>) -> Binding<T> {
        Binding(get: { record[keyPath: keyPath] }, set: { var updated = record; updated[keyPath: keyPath] = $0; update(updated) })
    }
    private func update(_ value: WorkbenchRecord) {
        if let recordID, let index = model.savedTabs.firstIndex(where: { $0.id == recordID }) { model.savedTabs[index] = value }
        else { model.primary = value }
    }
    private func importTranscript() {
        let picker = NSOpenPanel()
        picker.allowedContentTypes = [.plainText]
        picker.allowsMultipleSelection = false
        guard picker.runModal() == .OK, let source = picker.url else { return }
        do {
            let bytes = try source.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            guard bytes.isRegularFile == true, let count = bytes.fileSize, count <= 1_000_000 else {
                model.notice = "1 MB以内のテキストファイルを選んでください。"; return
            }
            let imported = try String(contentsOf: source, encoding: .utf8)
            if !record.transcript.isEmpty {
                let alert = NSAlert()
                alert.messageText = "表示中の文字起こしを置き換えますか？"
                alert.addButton(withTitle: "置き換える")
                alert.addButton(withTitle: "キャンセル")
                guard alert.runModal() == .alertFirstButtonReturn else { return }
            }
            var updated = record
            updated.transcript = imported
            update(updated)
            model.notice = "文字起こしを読み込みました。"
        } catch { model.notice = "文字起こしを読み込めませんでした。" }
    }
    private func exportText(_ body: String, name: String) {
        let picker = NSSavePanel(); picker.allowedContentTypes = [.plainText]; picker.nameFieldStringValue = name
        guard picker.runModal() == .OK, let target = picker.url else { return }
        do { try body.write(to: target, atomically: true, encoding: .utf8); model.notice = "TXTを書き出しました。" }
        catch { model.notice = "TXTを書き出せませんでした。" }
    }
    private struct CaptureApplication: Identifiable {
        let id: String
        let label: String
    }
    private func runningCaptureApplications() -> [CaptureApplication] {
        var labels: [String: String] = [:]
        for app in NSWorkspace.shared.runningApplications where app.activationPolicy == .regular {
            guard let id = app.bundleIdentifier, id != Bundle.main.bundleIdentifier, let label = app.localizedName else { continue }
            labels[id] = label
        }
        return labels.map { CaptureApplication(id: $0.key, label: $0.value) }.sorted {
            $0.label.localizedStandardCompare($1.label) == .orderedAscending
        }
    }
}

private struct CaptureConfirmation: View {
    let scope: String
    let saveRaw: Bool
    let confirm: () -> Void
    let cancel: () -> Void
    @State private var consent = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("録音を開始").font(.title2.bold())
            Text(scope)
            Text(saveRaw ? "文字起こしと原音をMac内に保存します。" : "文字起こしのみMac内に保存します。原音は残しません。")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("参加者の録音同意を確認", isOn: $consent)
            HStack {
                Spacer()
                Button("キャンセル", role: .cancel, action: cancel)
                Button("収音を開始", action: confirm).disabled(!consent)
                    .buttonStyle(MeetingActionButtonStyle(kind: .primary)).keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("meeting-confirm-capture")
            }
        }.padding(20).frame(width: 435).meetingInteractionDefaults()
    }
}

private struct MeetingNotesPane: View {
    @ObservedObject var model: WorkbenchWorkspace
    let recordID: UUID?
    @State private var showOpenItems = false
    private var record: WorkbenchRecord { recordID.flatMap { id in model.savedTabs.first { $0.id == id } } ?? model.primary }
    var body: some View {
        GeometryReader { geometry in
            VStack(alignment: .leading, spacing: 8) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Text("自分のメモ").font(.headline); Spacer()
                            Text("議事録の生成時に参照します").font(.caption).foregroundStyle(.secondary)
                        }
                        TextEditor(text: binding(\.notes)).font(.body).padding(8).frame(minHeight: 155)
                            .overlay(RoundedRectangle(cornerRadius: 8).stroke(.secondary.opacity(0.25)))
                            .accessibilityIdentifier("meeting-notes-editor")
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: max(130, min(geometry.size.height * 0.43, 360)))
                DisclosureGroup("分類文脈（同じ話題の過去議事録）") {
                    TextField("話題（任意）", text: binding(\.topic)).textFieldStyle(.roundedBorder)
                    Toggle("生成時に補助文脈として参照", isOn: binding(\.usesTopicContext))
                    Text("同じ話題で保存した議事録の一部だけを端末内AIの補助に使います。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Text("議事録").font(.headline)
                    Spacer()
                    Menu("作成") {
                        Button("端末内AIで作る") { Task { await model.generateOnDevice(recordID: recordID) } }
                            .disabled(record.transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || record.isRecording || record.isBusy)
                        Button("他のAI用の指示をコピー") { NativeMinutes.copyPrompt(transcript: record.transcript, notes: record.notes) }
                            .disabled(record.transcript.isEmpty)
                    }
                }
                HStack {
                    Picker("表示", selection: $showOpenItems) {
                        Text("本文").tag(false)
                        Text("未決事項").tag(true)
                    }.pickerStyle(.segmented).labelsHidden()
                    Spacer()
                    Button("コピー") { copyText(showOpenItems ? record.openItems : record.minutes) }
                        .disabled((showOpenItems ? record.openItems : record.minutes).isEmpty)
                }
                TextEditor(text: showOpenItems ? binding(\.openItems) : binding(\.minutes)).font(.body)
                    .accessibilityIdentifier("meeting-minutes-editor")
            }.padding(14)
        }
    }
    private func binding<T>(_ keyPath: WritableKeyPath<WorkbenchRecord, T>) -> Binding<T> {
        Binding(get: { record[keyPath: keyPath] }, set: { var updated = record; updated[keyPath: keyPath] = $0; update(updated) })
    }
    private func update(_ value: WorkbenchRecord) { if let id = recordID, let i = model.savedTabs.firstIndex(where: { $0.id == id }) { model.savedTabs[i] = value } else { model.primary = value } }
}

private struct MeetingSupportPane: View {
    @ObservedObject var model: WorkbenchWorkspace
    let recordID: UUID?
    private var record: WorkbenchRecord { recordID.flatMap { id in model.savedTabs.first { $0.id == id } } ?? model.primary }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("文字起こし").font(.headline); Spacer()
                if record.isBusy { ProgressView().controlSize(.small) }
                Button("コピー") { copyText(record.transcript) }.disabled(record.transcript.isEmpty)
            }
            ZStack(alignment: .topLeading) {
                TextEditor(text: binding(\.transcript)).font(.body)
                    .disabled(record.isRecording || record.isBusy)
                    .accessibilityIdentifier("meeting-transcript-editor")
                if record.transcript.isEmpty {
                    Text(record.isRecording ? "発言の認識を待っています…" : "録音またはTXTの取り込みで文字起こしを表示します")
                        .font(.callout).foregroundStyle(.secondary).padding(5).allowsHitTesting(false)
                }
            }
        }.padding(12)
    }
    private func binding<T>(_ keyPath: WritableKeyPath<WorkbenchRecord, T>) -> Binding<T> {
        Binding(get: { record[keyPath: keyPath] }, set: { var updated = record; updated[keyPath: keyPath] = $0; update(updated) })
    }
    private func update(_ value: WorkbenchRecord) { if let id = recordID, let i = model.savedTabs.firstIndex(where: { $0.id == id }) { model.savedTabs[i] = value } else { model.primary = value } }
}

@MainActor
func copyText(_ text: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}

private struct MeetingHistorySheet: View {
    @ObservedObject var model: WorkbenchWorkspace
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("保存済み会議").font(.headline)
            ScrollView {
                let groups = Dictionary(grouping: model.loadHistory()) { $0.topic.isEmpty ? "分類なし" : $0.topic }
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(groups.keys.sorted(), id: \.self) { topic in
                        Text(topic).font(.caption.bold()).foregroundStyle(.secondary)
                        ForEach(groups[topic] ?? []) { item in
                            Button("\(item.title) · \(item.transcript.count)文字") { model.addHistoryTab(item.id); model.showHistory = false }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(20).frame(width: 420, height: 360)
    }
}
