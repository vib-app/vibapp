// Project-authored read-only macOS window metadata for native acceptance.
// No UI input, activation, AppleScript, permission request or image synthesis.
import CoreGraphics
import Foundation

struct AuditWindow: Encodable {
    let id: Int
    let owner_pid: Int
    let owner: String
    let title: String
    let width: Int
    let height: Int
}

struct AuditWindows: Encodable {
    let schema_version = "vibapp.native-window-metadata-v1"
    let windows: [AuditWindow]
}

func fail(_ message: String) -> Never {
    fputs(message + "\n", stderr)
    exit(1)
}

guard CGPreflightScreenCaptureAccess() else {
    fail("Screen Recording permission must already be granted before this read-only audit")
}
guard let rows = CGWindowListCopyWindowInfo(
    [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
) as? [[String: Any]] else {
    fail("Cannot read OS window metadata")
}
var selected: [AuditWindow] = []
for row in rows {
    guard let owner = row[kCGWindowOwnerName as String] as? String,
          owner.lowercased().contains("vibapp") else { continue }
    guard let id = row[kCGWindowNumber as String] as? Int,
          let pid = row[kCGWindowOwnerPID as String] as? Int,
          let title = row[kCGWindowName as String] as? String,
          let bounds = row[kCGWindowBounds as String] as? [String: Any],
          let width = bounds["Width"] as? Double,
          let height = bounds["Height"] as? Double,
          id > 0, pid > 0, width > 0, height > 0, width.isFinite, height.isFinite,
          width <= 16384, height <= 16384,
          owner.count <= 1024, title.count <= 1024 else { continue }
    selected.append(AuditWindow(id: id, owner_pid: pid, owner: owner, title: title,
                                width: Int(width), height: Int(height)))
    if selected.count > 512 { fail("Too many matching OS windows") }
}
let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
do {
    let bytes = try encoder.encode(AuditWindows(windows: selected.sorted { $0.id < $1.id }))
    guard let output = String(data: bytes, encoding: .utf8) else { fail("Invalid window metadata encoding") }
    print(output)
} catch {
    fail("Cannot encode OS window metadata")
}
