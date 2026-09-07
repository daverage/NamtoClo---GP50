import SwiftUI

/// Shared error presentation for Convert/Upload failures. Deliberately a
/// `.sheet`, not a `.alert`: SwiftUI's Alert only renders plain `Text` in
/// its message body -- a `DisclosureGroup` (or any other interactive view)
/// placed there silently fails to render, which previously made the
/// "Technical details" section (with the actual namtoclo exit code/stderr)
/// invisible, so a real failure looked like "no error log" even though one
/// existed. A sheet has no such restriction.
struct ErrorSheetView: View {
    let title: String
    let error: BackendError
    let onDismiss: () -> Void
    @State private var detailsExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(title, systemImage: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.red)
            Text(error.summary)
                .fixedSize(horizontal: false, vertical: true)

            DisclosureGroup("Technical details", isExpanded: $detailsExpanded) {
                ScrollView {
                    Text(error.technicalDetails.isEmpty ? "(no additional detail)" : error.technicalDetails)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 180)
                .padding(.top, 4)
            }

            HStack {
                Spacer()
                Button("OK", action: onDismiss)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 440)
    }
}
