import AppKit
import Darwin
import Foundation
import SwiftUI

private let helperEnvironment = ["PATH": "/usr/bin:/bin", "LANG": "ja_JP.UTF-8"]
private let pcmChunkBytes = 16_000 * 2 * 30

struct WorkbenchRecord: Identifiable, Equatable {
    var id: UUID
    var title: String
    var notes: String = ""
    var openItems: String = ""
    var topic: String = ""
    var usesTopicContext = false
    var transcript: String
    var minutes: String
    var folder: URL?
    var isRecording = false
    var isBusy = false
}

@MainActor
final class WorkbenchWorkspace: ObservableObject {
    @Published var primary = WorkbenchRecord(id: UUID(), title: "新しい会議", transcript: "", minutes: "", folder: nil)
    @Published var savedTabs: [WorkbenchRecord] = []
    @Published var selectedID: UUID?
    @Published var historyPickerPresented = false
    @Published var remoteAudioOnly = false
    var includeMicrophone: Bool { !remoteAudioOnly }
    @Published var retainRawAudio = false
    @Published var mode: CaptureMode = .online
    @Published var applicationBundleID: String?
    @Published var notice = ""
    @Published var showStartConfirmation = false
    @Published var showHistory = false
    @Published var generationLeaseID: UUID?
    private var captureProcess: Process?
    private var pcmReaderTask: Task<Void, Never>?
    private var pcmDrainTask: Task<Void, Never>?
    private var captureErrorTask: Task<Data, Never>?
    private var activeCapture: CaptureRun?
    private var captureFailure: String?
    private var operatorStoppedCapture = false
    private let storageRoot: URL?

    init(storageRoot: URL? = nil) { self.storageRoot = storageRoot }

    private struct CaptureRun {
        let meetingID: UUID
        let folder: URL
        let mode: CaptureMode
        let includeMicrophone: Bool
        let retainRawAudio: Bool
        let applicationBundleID: String?
    }

    var hasActiveCapture: Bool { captureProcess != nil || activeCapture != nil }
    var captureScope: String {
        switch mode {
        case .inPerson: return "対面：マイクのみ"
        case .online:
            let source = applicationBundleID.flatMap { NSRunningApplication.runningApplications(withBundleIdentifier: $0).first?.localizedName }
                ?? (applicationBundleID == nil ? "Mac" : "指定したアプリ")
            return includeMicrophone ? "オンライン：\(source)の再生音＋マイク" : "オンライン：\(source)の再生音のみ"
        }
    }

    static func captureArguments(mode: CaptureMode, includeMicrophone: Bool, retainRawAudio: Bool, outputDirectory: URL,
                                 applicationBundleID: String? = nil) -> [String] {
        var args = retainRawAudio ? ["--output", outputDirectory.path] : ["--pcm-stdout"]
        args.append("--confirm")
        switch mode {
        case .inPerson:
            args.append("--microphone-only")
        case .online:
            if let applicationBundleID { args += ["--app-bundle-id", applicationBundleID] }
            else { args.append("--whole-mac") }
            if includeMicrophone { args.append("--include-microphone") }
        }
        return args
    }

    static func selfTestCaptureArguments() -> Bool {
        let folder = URL(fileURLWithPath: "/tmp/workbench-capture-contract")
        let workspace = WorkbenchWorkspace()
        guard !workspace.remoteAudioOnly, workspace.includeMicrophone,
              captureArguments(mode: .online, includeMicrophone: workspace.includeMicrophone, retainRawAudio: false, outputDirectory: folder)
                == ["--pcm-stdout", "--confirm", "--whole-mac", "--include-microphone"] else { return false }
        workspace.remoteAudioOnly = true
        return !workspace.includeMicrophone &&
        captureArguments(mode: .inPerson, includeMicrophone: workspace.includeMicrophone, retainRawAudio: false, outputDirectory: folder) == ["--pcm-stdout", "--confirm", "--microphone-only"] &&
        captureArguments(mode: .online, includeMicrophone: false, retainRawAudio: false, outputDirectory: folder) == ["--pcm-stdout", "--confirm", "--whole-mac"] &&
        captureArguments(mode: .online, includeMicrophone: true, retainRawAudio: true, outputDirectory: folder) == ["--output", folder.path, "--confirm", "--whole-mac", "--include-microphone"] &&
        captureArguments(mode: .online, includeMicrophone: false, retainRawAudio: false, outputDirectory: folder, applicationBundleID: "org.example.sample") == ["--pcm-stdout", "--confirm", "--app-bundle-id", "org.example.sample"]
    }

    enum CaptureMode: String, CaseIterable, Identifiable {
        case inPerson = "対面", online = "オンライン"
        var id: String { rawValue }
    }

    var selectedModel: WorkbenchRecord {
        get { selectedID.flatMap { id in savedTabs.first { $0.id == id } } ?? primary }
        set {
            if let id = selectedID, let index = savedTabs.firstIndex(where: { $0.id == id }) { savedTabs[index] = newValue }
            else { primary = newValue }
        }
    }

    func select(_ id: UUID?) { selectedID = id }
    func presentHistoryPicker() { historyPickerPresented = true }
    func closeHistory(_ id: UUID) {
        guard generationLeaseID != id else { notice = "このタブは議事録の作成中です。完了後に閉じられます。"; return }
        savedTabs.removeAll { $0.id == id }; if selectedID == id { selectedID = nil }
    }

    private var dataRoot: URL {
        if let storageRoot { return storageRoot }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("MeetingMinutesWorkbench/Meetings", isDirectory: true)
    }

    private var historyFolders: [URL] {
        let dirs = (try? FileManager.default.contentsOfDirectory(at: dataRoot, includingPropertiesForKeys: [.isDirectoryKey, .contentModificationDateKey])) ?? []
        return dirs.filter { UUID(uuidString: $0.lastPathComponent) != nil && (try? isPhysicalDirectory($0)) == true }
            .map { folder in
                (folder, (try? folder.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast)
            }.sorted { $0.1 > $1.1 }.map(\.0)
    }

    func loadHistory() -> [WorkbenchRecord] {
        historyFolders.compactMap { folder in
            guard let id = UUID(uuidString: folder.lastPathComponent),
                  (try? folder.appendingPathComponent("transcript.txt").resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]))
                    .map({ $0.isRegularFile == true && $0.isSymbolicLink != true }) == true,
                  let transcript = try? String(contentsOf: folder.appendingPathComponent("transcript.txt"), encoding: .utf8) else { return nil }
            let minutes = (try? String(contentsOf: folder.appendingPathComponent("minutes.txt"), encoding: .utf8)) ?? ""
            let title = (try? String(contentsOf: folder.appendingPathComponent("title.txt"), encoding: .utf8)) ?? "保存済み会議"
            let notes = (try? String(contentsOf: folder.appendingPathComponent("notes.txt"), encoding: .utf8)) ?? ""
            let openItems = (try? String(contentsOf: folder.appendingPathComponent("open-items.txt"), encoding: .utf8)) ?? ""
            let topic = readTextPrefix(in: folder, name: "topic.txt", maxBytes: 512)
            let contextual = readTextPrefix(in: folder, name: "topic-context.txt", maxBytes: 8) == "on"
            return WorkbenchRecord(id: id, title: title, notes: notes, openItems: openItems, topic: topic, usesTopicContext: contextual,
                                   transcript: transcript, minutes: minutes, folder: folder)
        }
    }

    func previousTopicContext(for current: WorkbenchRecord) -> String {
        let topic = current.topic.trimmingCharacters(in: .whitespacesAndNewlines)
        guard current.usesTopicContext, !topic.isEmpty else { return "" }
        var excerpts: [String] = []
        for folder in historyFolders where folder.lastPathComponent != current.id.uuidString {
            guard readTextPrefix(in: folder, name: "topic.txt", maxBytes: 512) == topic,
                  !readTextPrefix(in: folder, name: "transcript.txt", maxBytes: 12).isEmpty else { continue }
            let excerpt = String(readTextPrefix(in: folder, name: "minutes.txt", maxBytes: 1_800).prefix(450))
            if !excerpt.isEmpty { excerpts.append(excerpt) }
            if excerpts.count == 3 { break }
        }
        return excerpts.joined(separator: "\n\n")
    }

    func addHistoryTab(_ id: UUID) {
        guard !savedTabs.contains(where: { $0.id == id }), let record = loadHistory().first(where: { $0.id == id }) else { return }
        guard savedTabs.count < 6 else { notice = "保存済み会議タブは最大6件です"; return }
        savedTabs.append(record); selectedID = id; historyPickerPresented = false
    }

    func startRecording() {
        guard !hasActiveCapture else { notice = "収音中または停止・保存処理中です"; return }
        guard selectedID == nil else { notice = "履歴タブからは収音を開始できません"; return }
        guard primary.transcript.isEmpty else { notice = "この会議には原文があります。新しい会議を開始してください"; return }
        showStartConfirmation = true
    }
    func confirmStartRecording() {
        showStartConfirmation = false
        guard !hasActiveCapture else { notice = "収音中または処理中です"; return }
        guard selectedID == nil, primary.transcript.isEmpty else { notice = "現在の会議は収音を開始できない状態です"; return }
        do {
            try createPrivateDirectory(dataRoot)
            var meetingID = primary.id
            var folder = dataRoot.appendingPathComponent(meetingID.uuidString, isDirectory: true)
            if FileManager.default.fileExists(atPath: folder.path) {
                meetingID = UUID()
                folder = dataRoot.appendingPathComponent(meetingID.uuidString, isDirectory: true)
                guard !FileManager.default.fileExists(atPath: folder.path) else { throw WorkbenchError.captureFolderCollision }
                primary.id = meetingID
            }
            let run = CaptureRun(meetingID: meetingID, folder: folder, mode: mode, includeMicrophone: includeMicrophone,
                                 retainRawAudio: retainRawAudio, applicationBundleID: applicationBundleID)
            let process = Process()
            process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/workbench-system-audio")
            process.environment = helperEnvironment
            process.arguments = Self.captureArguments(mode: run.mode, includeMicrophone: run.includeMicrophone,
                                                      retainRawAudio: run.retainRawAudio, outputDirectory: run.folder,
                                                      applicationBundleID: run.applicationBundleID)
            let stderr = Pipe(); process.standardError = stderr
            if !run.retainRawAudio { process.standardOutput = Pipe() }
            else if FileManager.default.fileExists(atPath: folder.path) { throw WorkbenchError.captureFolderCollision }
            captureFailure = nil
            operatorStoppedCapture = false
            try process.run()
            captureErrorTask = Task.detached(priority: .utility) { readBounded(stderr.fileHandleForReading, limit: 16_384) }
            activeCapture = run; captureProcess = process; primary.id = meetingID; primary.folder = folder
            primary.isRecording = true; primary.isBusy = false; primary.transcript = ""
            notice = ""
            if !run.retainRawAudio, let pipe = process.standardOutput as? Pipe {
                // Keep the helper drained during recognition; a bounded queue prevents
                // RAM growth without silently losing any later intervals.
                let (chunks, continuation) = AsyncStream.makeStream(of: Data.self, bufferingPolicy: .bufferingOldest(4))
                pcmDrainTask = Task.detached(priority: .userInitiated) { [weak self] in
                    let handle = pipe.fileHandleForReading
                    var buffered = Data()
                    var overflow = false
                    while !Task.isCancelled {
                        let data = handle.availableData
                        if data.isEmpty { break }
                        if overflow { continue }
                        buffered.append(data)
                        while buffered.count >= pcmChunkBytes {
                            let chunk = Data(buffered.prefix(pcmChunkBytes)); buffered.removeFirst(pcmChunkBytes)
                            if case .dropped = continuation.yield(chunk) {
                                overflow = true
                                process.interrupt()
                                break
                            }
                        }
                    }
                    if !overflow && !buffered.isEmpty, case .dropped = continuation.yield(buffered) { overflow = true }
                    if overflow { await self?.reportPCMOverflow(run: run) }
                    continuation.finish()
                }
                pcmReaderTask = Task.detached(priority: .userInitiated) { [weak self] in
                    let recognizer = PCMRecognitionProcessor()
                    for await chunk in chunks {
                        await self?.receivePCM(chunk, for: run, recognizer: recognizer)
                    }
                    process.waitUntilExit()
                    let errorData = await self?.captureErrorTask?.value ?? Data()
                    await self?.finishPCMRecording(run: run, status: process.terminationStatus, errorData: errorData)
                }
            } else {
                pcmReaderTask = Task.detached(priority: .userInitiated) { [weak self] in
                    process.waitUntilExit()
                    let errorData = await self?.captureErrorTask?.value ?? Data()
                    await self?.rawCaptureExited(run: run, status: process.terminationStatus, errorData: errorData)
                }
            }
        } catch { activeCapture = nil; captureProcess = nil; primary.isRecording = false; primary.isBusy = false; notice = "収音を開始できません: \(error.localizedDescription)" }
    }

    func stopRecording() {
        guard let process = captureProcess else { return }
        operatorStoppedCapture = true
        primary.isBusy = true
        if process.isRunning { process.interrupt() }
    }

    private func reportPCMOverflow(run: CaptureRun) {
        guard activeCapture?.meetingID == run.meetingID else { return }
        captureFailure = "文字起こしが収音に追いつかず、未保存の区間があります"
        notice = captureFailure ?? ""
    }

    private func receivePCM(_ data: Data, for run: CaptureRun, recognizer: PCMRecognitionProcessor) async {
        guard activeCapture?.meetingID == run.meetingID,
              primary.id == run.meetingID else { return }
        do {
            let recognized = try await recognizer.recognize(data)
            guard !recognized.isEmpty else { return }
            guard activeCapture?.meetingID == run.meetingID, primary.id == run.meetingID else { return }
            try createPrivateDirectory(run.folder)
            let transcript = primary.transcript + recognized
            try savePrivateText(transcript, to: run.folder.appendingPathComponent("transcript.txt"))
            primary.transcript = transcript
        } catch {
            captureFailure = captureFailure ?? error.localizedDescription
            notice = "音声認識または原文保存に失敗しました。既に保存した原文は保持しています。"
            if let captureProcess, captureProcess.isRunning { captureProcess.interrupt() }
        }
    }

    private func finishPCMRecording(run: CaptureRun, status: Int32, errorData: Data) async {
        guard activeCapture?.meetingID == run.meetingID, primary.id == run.meetingID else { return }
        primary.isBusy = true
        primary.isRecording = false
        do {
            if let captureFailure { throw WorkbenchError.transcriptionHelperFailed(captureFailure) }
            guard !primary.transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                let detail = String(data: errorData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                throw status == 0 ? WorkbenchError.transcriptionFailed : WorkbenchError.captureHelperFailed(detail)
            }
            try saveTitleAndMinutes(recordID: run.meetingID)
            guard status == 0, operatorStoppedCapture else { throw WorkbenchError.captureHelperFailed(String(data: errorData, encoding: .utf8) ?? "") }
            notice = "原音を保存せず、文字起こしTXTを保存しました"
        } catch { notice = "停止後の処理に失敗しました: \(error.localizedDescription)。保存済みの原文と収音先は保持しています" }
        primary.isRecording = false; primary.isBusy = false; captureProcess = nil; activeCapture = nil; pcmReaderTask = nil; pcmDrainTask = nil; captureErrorTask = nil
    }

    private func rawCaptureExited(run: CaptureRun, status: Int32, errorData: Data) async {
        guard activeCapture?.meetingID == run.meetingID, primary.id == run.meetingID else { return }
        primary.isRecording = false
        primary.isBusy = true
        do {
            guard status == 0, operatorStoppedCapture else { throw WorkbenchError.captureHelperFailed(String(data: errorData, encoding: .utf8) ?? "") }
            await finishCapture(run: run)
        } catch { primary.isBusy = false; captureProcess = nil; activeCapture = nil; pcmReaderTask = nil; captureErrorTask = nil; notice = "収音に失敗しました: \(error.localizedDescription)。音声データがあれば保持しています" }
    }

    private func finishCapture(run: CaptureRun) async {
        let transcriber = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/workbench-apple-transcribe")
        let output = run.folder.appendingPathComponent("transcript.txt")
        let process = Process(); process.executableURL = transcriber
        process.environment = helperEnvironment
        process.arguments = ["--input", run.folder.path, "--output", output.path, "--language", "ja"]
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let status = try await waitForProcess(process, timeoutSeconds: 43_200)
            guard status == 0 else { throw WorkbenchError.transcriptionFailed }
            let text = try String(contentsOf: output, encoding: .utf8)
            primary.transcript = text
            try saveTitleAndMinutes(recordID: run.meetingID)
            notice = "文字起こしを保存しました"
        } catch { notice = "文字起こしを保存できません。収音データを保持しています: \(error.localizedDescription)" }
        primary.isBusy = false; captureProcess = nil; activeCapture = nil; pcmReaderTask = nil; captureErrorTask = nil
    }

    func saveTitleAndMinutes(recordID: UUID? = nil) throws {
        let key = recordID ?? primary.id
        let index = savedTabs.firstIndex { $0.id == key }
        let selected = key == primary.id ? primary : index.map { savedTabs[$0] }
        guard var target = selected else { throw WorkbenchError.meetingUnavailable }
        let folder: URL
        if let existing = target.folder { folder = existing }
        else { folder = dataRoot.appendingPathComponent(target.id.uuidString, isDirectory: true) }
        try createPrivateDirectory(folder)
        target.folder = folder
        target.topic = String(target.topic.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        try savePrivateText(target.title, to: folder.appendingPathComponent("title.txt"))
        try savePrivateText(target.notes, to: folder.appendingPathComponent("notes.txt"))
        try savePrivateText(target.openItems, to: folder.appendingPathComponent("open-items.txt"))
        try savePrivateText(target.minutes, to: folder.appendingPathComponent("minutes.txt"))
        try savePrivateText(target.transcript, to: folder.appendingPathComponent("transcript.txt"))
        try savePrivateText(target.topic, to: folder.appendingPathComponent("topic.txt"))
        try savePrivateText(target.usesTopicContext ? "on" : "off", to: folder.appendingPathComponent("topic-context.txt"))
        if key == primary.id { primary = target }
        else if let index { savedTabs[index] = target }
    }

    func saveTitle(recordID: UUID? = nil) { do { try saveTitleAndMinutes(recordID: recordID); notice = "保存しました" } catch { notice = "保存できません: \(error.localizedDescription)" } }

    static func selfTestPersistence() throws -> Bool {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("workbench-test-" + UUID().uuidString)
        try createPrivateDirectory(root)
        defer { try? FileManager.default.removeItem(at: root) }
        let fixture = WorkbenchWorkspace(storageRoot: root)
        let id = fixture.primary.id
        fixture.primary.title = "合成会議"
        fixture.primary.notes = "架空のメモ"
        fixture.primary.topic = "架空の話題"
        fixture.primary.transcript = "合成原文：リンゴ\n"
        fixture.primary.openItems = "未決事項の例"
        fixture.primary.minutes = "決定の例"
        try fixture.saveTitleAndMinutes(recordID: id)
        guard let original = fixture.loadHistory().first, let folder = original.folder,
              original.id == id, original.title == fixture.primary.title, original.notes == fixture.primary.notes,
              original.openItems == fixture.primary.openItems, original.minutes == fixture.primary.minutes,
              original.transcript == fixture.primary.transcript, original.topic == "架空の話題",
              !original.usesTopicContext else {
            print("FAIL: record round-trip")
            return false
        }
        let attrs = try FileManager.default.attributesOfItem(atPath: folder.appendingPathComponent("transcript.txt").path)
        guard (attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600,
              Set(try FileManager.default.contentsOfDirectory(atPath: folder.path))
                == ["title.txt", "notes.txt", "transcript.txt", "minutes.txt", "open-items.txt", "topic.txt", "topic-context.txt"] else {
            print("FAIL: private TXT permissions or layout")
            return false
        }
        fixture.createNewMeeting()
        fixture.primary.topic = "架空の話題"
        guard fixture.previousTopicContext(for: fixture.primary).isEmpty else { return false }
        fixture.primary.usesTopicContext = true
        guard fixture.previousTopicContext(for: fixture.primary) == "決定の例" else { return false }
        fixture.primary.topic = "無関係"
        guard fixture.previousTopicContext(for: fixture.primary).isEmpty else { return false }
        fixture.addHistoryTab(id)
        fixture.savedTabs[0].minutes = "履歴だけ更新"
        try fixture.saveTitleAndMinutes(recordID: id)
        guard fixture.loadHistory().first?.minutes == "履歴だけ更新", fixture.primary.minutes.isEmpty else {
            print("FAIL: history tab update isolation")
            return false
        }
        do { try fixture.saveTitleAndMinutes(recordID: UUID()); return false }
        catch WorkbenchError.meetingUnavailable { return true }
    }
    func createNewMeeting() {
        guard !hasActiveCapture, !primary.isRecording, !primary.isBusy else { notice = "処理が終わってから新しい会議を始めてください"; return }
        primary = WorkbenchRecord(id: UUID(), title: "新しい会議", transcript: "", minutes: "", folder: nil); selectedID = nil
    }
    func exportMinutes() {
        let panel = NSSavePanel(); panel.allowedContentTypes = [.plainText]; panel.nameFieldStringValue = "meeting-minutes.txt"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { try selectedModel.minutes.write(to: url, atomically: true, encoding: .utf8); notice = "TXTを書き出しました" }
        catch { notice = "書き出せません: \(error.localizedDescription)" }
    }
}

enum WorkbenchError: LocalizedError {
    case transcriptionFailed, transcriptionOutputTooLarge, captureFailed, captureFolderCollision, processTimeout
    case captureHelperFailed(String), transcriptionHelperFailed(String)
    case meetingUnavailable, unsafeStoragePath
    var errorDescription: String? {
        switch self {
        case .transcriptionFailed: "文字起こしヘルパーが失敗しました"
        case .transcriptionOutputTooLarge: "文字起こしの出力が上限を超えました"
        case .captureFailed: "収音ヘルパーが正常終了しませんでした"
        case .captureFolderCollision: "保存先が既に使用されています。新しい会議を作成してください"
        case .processTimeout: "文字起こし処理が時間内に完了しませんでした"
        case .meetingUnavailable: "保存対象の会議が見つかりません"
        case .unsafeStoragePath: "保存先が通常のフォルダではありません"
        case .captureHelperFailed(let detail): detail.isEmpty ? "収音ヘルパーが正常終了しませんでした" : "収音ヘルパー: \(detail)"
        case .transcriptionHelperFailed(let detail): detail.isEmpty ? "文字起こしヘルパーが失敗しました" : "文字起こしヘルパー: \(detail)"
        }
    }
}

private func isPhysicalDirectory(_ folder: URL) throws -> Bool {
    let values = try folder.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    return values.isDirectory == true && values.isSymbolicLink != true
}

private func readTextPrefix(in folder: URL, name: String, maxBytes: Int) -> String {
    let file = folder.appendingPathComponent(name)
    guard let metadata = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
          metadata.isRegularFile == true, metadata.isSymbolicLink != true,
          let handle = try? FileHandle(forReadingFrom: file) else { return "" }
    defer { try? handle.close() }
    guard let data = try? handle.read(upToCount: maxBytes) else { return "" }
    return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
}

private func createPrivateDirectory(_ folder: URL) throws {
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                           attributes: [.posixPermissions: 0o700])
    guard try isPhysicalDirectory(folder) else { throw WorkbenchError.unsafeStoragePath }
}

private func savePrivateText(_ text: String, to target: URL) throws {
    let folder = target.deletingLastPathComponent()
    guard try isPhysicalDirectory(folder) else { throw WorkbenchError.unsafeStoragePath }
    let temporary = folder.appendingPathComponent(".write-" + UUID().uuidString)
    let descriptor = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    defer { _ = unlink(temporary.path) }
    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    try handle.write(contentsOf: Data(text.utf8))
    try handle.synchronize()
    try handle.close()
    guard rename(temporary.path, target.path) == 0 else {
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
    let directory = open(folder.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    guard directory >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    defer { _ = close(directory) }
    guard fsync(directory) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
}

private actor PCMRecognitionProcessor {
    private let helper = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/workbench-apple-transcribe")

    func recognize(_ pcm: Data) async throws -> String {
        let process = Process(); process.executableURL = helper
        process.environment = helperEnvironment
        process.arguments = ["--pcm-stdin", "--allow-empty", "--language", "ja"]
        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.standardInput = input; process.standardOutput = output; process.standardError = errors
        try process.run()
        let outputTask = Task.detached(priority: .utility) { readBounded(output.fileHandleForReading, limit: 1_000_001) }
        let errorTask = Task.detached(priority: .utility) { readBounded(errors.fileHandleForReading, limit: 16_384) }
        let deadline = ContinuousClock.now.advanced(by: .seconds(120))
        let status = try await writeInputAndWait(pcm, input: input.fileHandleForWriting, process: process, deadline: deadline)
        let errorData = await errorTask.value
        guard status == 0 else { throw WorkbenchError.transcriptionHelperFailed(String(data: errorData, encoding: .utf8) ?? "") }
        let data = await outputTask.value
        guard data.count <= 1_000_000 else { throw WorkbenchError.transcriptionOutputTooLarge }
        let text = String(data: data, encoding: .utf8) ?? ""
        return text.isEmpty ? "" : text + "\n"
    }

}

private func writeInputAndWait(_ data: Data, input: FileHandle, process: Process, deadline: ContinuousClock.Instant) async throws -> Int32 {
    do {
        let fd = input.fileDescriptor
        let originalFlags = fcntl(fd, F_GETFL)
        guard originalFlags >= 0, fcntl(fd, F_SETFL, originalFlags | O_NONBLOCK) == 0,
              fcntl(fd, F_SETNOSIGPIPE, 1) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var offset = 0
        while offset < data.count && process.isRunning {
            guard ContinuousClock.now < deadline else { throw WorkbenchError.processTimeout }
            let (count, code) = data.withUnsafeBytes { bytes -> (Int, Int32) in
                let written = Darwin.write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                return (written, written < 0 ? errno : 0)
            }
            if count > 0 { offset += count }
            else if code == EPIPE { break }
            else if code == EAGAIN || code == EWOULDBLOCK || code == EINTR || count == 0 {
                try await Task.sleep(for: .milliseconds(20))
            } else { throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO) }
        }
        try input.close()
        return try await waitForProcess(process, until: deadline)
    } catch {
        try? input.close()
        await terminateHelper(process)
        throw error
    }
}

func selfTestStalledTranscriber() async -> Bool {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: "/bin/sleep")
    process.arguments = ["15"]
    process.standardInput = pipe
    do {
        try process.run()
        _ = try await writeInputAndWait(Data(repeating: 0, count: 960_000), input: pipe.fileHandleForWriting,
                                        process: process, deadline: .now.advanced(by: .milliseconds(200)))
        return false
    } catch WorkbenchError.processTimeout {
        return !process.isRunning
    } catch { print("FAIL: helper stdin test: \(error)"); return false }
}

private func readBounded(_ handle: FileHandle, limit: Int) -> Data {
    var output = Data()
    while true {
        let chunk = handle.availableData
        if chunk.isEmpty { return output }
        if output.count < limit { output.append(chunk.prefix(limit - output.count)) }
    }
}

private func waitForProcess(_ process: Process, timeoutSeconds: Int) async throws -> Int32 {
    try await waitForProcess(process, until: .now.advanced(by: .seconds(timeoutSeconds)))
}

private func waitForProcess(_ process: Process, until deadline: ContinuousClock.Instant) async throws -> Int32 {
    while process.isRunning && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(100)) }
    guard !process.isRunning else {
        await terminateHelper(process)
        throw WorkbenchError.processTimeout
    }
    return process.terminationStatus
}

private func terminateHelper(_ process: Process) async {
    guard process.isRunning else { return }
    process.terminate()
    let deadline = ContinuousClock.now.advanced(by: .seconds(2))
    while process.isRunning && ContinuousClock.now < deadline {
        try? await Task.sleep(for: .milliseconds(50))
    }
    if process.isRunning {
        _ = Darwin.kill(process.processIdentifier, SIGKILL)
        process.waitUntilExit()
    }
}
