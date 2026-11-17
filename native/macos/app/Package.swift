// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "MeetingMinutesWorkbench",
    platforms: [.macOS(.v26)],
    products: [.executable(name: "MeetingMinutesWorkbench", targets: ["MeetingMinutesWorkbench"])],
    targets: [.executableTarget(name: "MeetingMinutesWorkbench")]
)
