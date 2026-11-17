import SwiftUI
import AppKit

/// Keep native button roles, focus and keyboard handling; only add a cursor region.
struct MeetingActionButtonStyle: PrimitiveButtonStyle {
    enum Kind { case secondary, primary, plain, borderless, link }
    var kind: Kind = .secondary

    @ViewBuilder func makeBody(configuration: Configuration) -> some View {
        Group {
            switch kind {
            case .secondary: Button(configuration).buttonStyle(.bordered)
            case .primary: Button(configuration).buttonStyle(.borderedProminent)
            case .plain: Button(configuration).buttonStyle(.plain)
            case .borderless: Button(configuration).buttonStyle(.borderless)
            case .link: Button(configuration).buttonStyle(.link)
            }
        }.meetingActionCursor()
    }
}

struct MeetingInteractionDefaults: ViewModifier {
    func body(content: Content) -> some View {
        content
            .buttonStyle(MeetingActionButtonStyle())
            .menuStyle(MeetingActionMenuStyle())
            .toggleStyle(MeetingFormCheckboxStyle())
            .disclosureGroupStyle(MeetingFormDisclosureStyle())
    }
}

struct MeetingActionMenuStyle: MenuStyle {
    func makeBody(configuration: Configuration) -> some View {
        Menu(configuration).menuStyle(.automatic).meetingActionCursor()
    }
}

extension View {
    func meetingInteractionDefaults() -> some View { modifier(MeetingInteractionDefaults()) }
    func meetingActionCursor() -> some View { modifier(MeetingActionCursor()) }
    func meetingHoverSurface() -> some View { modifier(MeetingHoverSurface()) }
}

private struct MeetingActionCursor: ViewModifier {
    @Environment(\.isEnabled) private var enabled
    func body(content: Content) -> some View {
        content.background(MeetingCursorRegion(enabled: enabled).accessibilityHidden(true))
    }
}

private struct MeetingCursorRegion: NSViewRepresentable {
    let enabled: Bool
    func makeNSView(context: Context) -> MeetingCursorRegionView { MeetingCursorRegionView() }
    func updateNSView(_ view: MeetingCursorRegionView, context: Context) {
        guard view.actionEnabled != enabled else { return }
        view.actionEnabled = enabled
        view.window?.invalidateCursorRects(for: view)
    }
}

/// No event monitor, cursor stack or timer. Removing the view removes its cursor rect.
final class MeetingCursorRegionView: NSView {
    var actionEnabled = false
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func resetCursorRects() {
        let rect = Self.actionRect(bounds: bounds, visible: visibleRect, enabled: actionEnabled)
        if !rect.isEmpty { addCursorRect(rect, cursor: .pointingHand) }
    }
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        window?.invalidateCursorRects(for: self)
    }
    override func setFrameSize(_ newSize: NSSize) {
        let changed = frame.size != newSize
        super.setFrameSize(newSize)
        if changed { window?.invalidateCursorRects(for: self) }
    }
    static func actionRect(bounds: NSRect, visible: NSRect, enabled: Bool) -> NSRect {
        guard enabled else { return .zero }
        let rect = bounds.intersection(visible)
        return rect.isNull || rect.isEmpty ? .zero : rect
    }
}

private struct MeetingHoverSurface: ViewModifier {
    @Environment(\.isEnabled) private var enabled
    @State private var hovering = false
    func body(content: Content) -> some View {
        content
            .background(RoundedRectangle(cornerRadius: 4).fill(.primary.opacity(enabled && hovering ? 0.055 : 0)))
            .onHover { hovering = $0 }
    }
}

/// One button owns each header; the content below it is never a tap target.
struct MeetingFormDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                configuration.isExpanded.toggle()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: configuration.isExpanded ? "chevron.down" : "chevron.right")
                        .font(.caption).foregroundStyle(.secondary).accessibilityHidden(true)
                    configuration.label
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, minHeight: 32, alignment: .leading)
                .contentShape(Rectangle())
                .meetingHoverSurface()
            }
            .buttonStyle(MeetingActionButtonStyle(kind: .plain))
            .accessibilityValue(configuration.isExpanded ? "展開中" : "折りたたみ")
            if configuration.isExpanded {
                // SwiftUI resets a custom disclosure style inside its own content.
                // Reapply only to descendants so nested settings keep the same target.
                configuration.content.disclosureGroupStyle(MeetingFormDisclosureStyle())
            }
        }
    }
}

struct MeetingFormCheckboxStyle: ToggleStyle {
    @Environment(\.isEnabled) private var enabled
    var fillsWidth = false
    func makeBody(configuration: Configuration) -> some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: configuration.isOn ? "checkmark.square.fill" : "square")
                    .foregroundStyle(configuration.isOn && enabled ? Color.accentColor : Color.secondary)
                    .accessibilityHidden(true)
                configuration.label
                if fillsWidth { Spacer(minLength: 0) }
            }
            .padding(.horizontal, 3)
            .frame(maxWidth: fillsWidth ? .infinity : nil, minHeight: 32, alignment: .leading)
            .contentShape(Rectangle())
            .meetingHoverSurface()
        }
        .buttonStyle(MeetingActionButtonStyle(kind: .plain))
        .accessibilityRepresentation {
            Toggle(isOn: configuration.$isOn) { configuration.label }.toggleStyle(.checkbox)
        }
    }
}
