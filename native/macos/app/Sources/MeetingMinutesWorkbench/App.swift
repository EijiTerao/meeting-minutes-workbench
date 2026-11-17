import AppKit
import Darwin
import SwiftUI

@main
struct MeetingMinutesWorkbenchApp: App {
    @StateObject private var workspace = WorkbenchWorkspace()
    @NSApplicationDelegateAdaptor(WorkbenchTerminationGuard.self) private var termination
    init() {
        if CommandLine.arguments.contains("--self-test") {
            guard WorkbenchWorkspace.selfTestCaptureArguments(),
                  NativeMinutes.section("次回", ["11月22日に開催"]) == "【次回】\n・11月22日に開催",
                  NativeMinutes.section("未決事項", [" "]) == nil else {
                print("FAIL: capture arguments or output format"); Darwin.exit(1)
            }
            do { guard try WorkbenchWorkspace.selfTestPersistence() else { Darwin.exit(1) } }
            catch { print("FAIL: private TXT round-trip: \(error)"); Darwin.exit(1) }
            Task.detached {
                let timeoutPassed = await selfTestStalledTranscriber()
                if timeoutPassed { print("PASS: capture scope, private TXT round-trip, helper timeout, minutes format") }
                Darwin.exit(timeoutPassed ? 0 : 1)
            }
            dispatchMain()
        }
    }
    var body: some Scene {
        Window("Meeting Minutes Workbench", id: "meeting-workspace") {
            MeetingView(workspace: workspace)
                .background(WorkbenchWindowBinding(workspace: workspace).frame(width: 0, height: 0))
                .onAppear { termination.workspace = workspace }
        }
        .defaultSize(width: 1000, height: 720)
    }
}

@MainActor
private final class WorkbenchTerminationGuard: NSObject, NSApplicationDelegate {
    weak var workspace: WorkbenchWorkspace?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        workspace?.canCloseWindow() == false ? .terminateCancel : .terminateNow
    }
}

extension WorkbenchWorkspace {
    @MainActor
    fileprivate func canCloseWindow() -> Bool {
        guard !hasActiveCapture else {
            notice = "録音中または保存中です。「録音を停止」を押し、保存結果を確認してから閉じてください。"
            return false
        }
        guard generationLeaseID == nil else {
            notice = "議事録の作成が終わってから閉じてください。"
            return false
        }
        return true
    }
}

private struct WorkbenchWindowBinding: NSViewRepresentable {
    let workspace: WorkbenchWorkspace
    func makeNSView(context: Context) -> ClosingView { ClosingView(workspace: workspace) }
    func updateNSView(_ nsView: ClosingView, context: Context) { nsView.workspace = workspace }

    final class ClosingView: NSView {
        weak var workspace: WorkbenchWorkspace?
        private let proxy = WorkbenchWindowDelegate()
        init(workspace: WorkbenchWorkspace) { self.workspace = workspace; super.init(frame: .zero) }
        required init?(coder: NSCoder) { nil }
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, window.delegate !== proxy else { return }
            proxy.forwardingDelegate = window.delegate
            proxy.workspace = workspace
            window.delegate = proxy
        }
    }
}

// Forward unrelated delegate methods so the native window keeps its normal behaviour.
private final class WorkbenchWeakDelegate: @unchecked Sendable {
    private let lock = NSLock()
    private weak var value: (any NSWindowDelegate)?
    func get() -> (any NSWindowDelegate)? { lock.withLock { value } }
    func set(_ delegate: (any NSWindowDelegate)?) { lock.withLock { value = delegate } }
}

@MainActor
private final class WorkbenchWindowDelegate: NSObject, NSWindowDelegate {
    private nonisolated let forwarding = WorkbenchWeakDelegate()
    var forwardingDelegate: (any NSWindowDelegate)? {
        get { forwarding.get() }
        set { forwarding.set(newValue) }
    }
    weak var workspace: WorkbenchWorkspace?
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard workspace?.canCloseWindow() != false else { return false }
        return forwardingDelegate?.windowShouldClose?(sender) ?? true
    }
    override func responds(to selector: Selector!) -> Bool {
        super.responds(to: selector) || forwarding.get()?.responds(to: selector) == true
    }
    override func forwardingTarget(for selector: Selector!) -> Any? {
        if let target = forwarding.get(), target.responds(to: selector) { return target }
        return super.forwardingTarget(for: selector)
    }
}
