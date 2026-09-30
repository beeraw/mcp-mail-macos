// Extracts the text layer of PDF files with PDFKit.
//
// Usage: pdftext [--max-pages N] [--max-chars N] file...
// Prints one JSON object per file: {"path", "pages", "text", "error"}.
// Several files per invocation amortise the process start; a failure on one
// file never stops the batch.
import Foundation
import PDFKit

var maxPages = 50
var maxChars = 100_000
var paths: [String] = []
var arguments = CommandLine.arguments.dropFirst().makeIterator()
while let argument = arguments.next() {
    switch argument {
    case "--max-pages": maxPages = Int(arguments.next() ?? "") ?? maxPages
    case "--max-chars": maxChars = Int(arguments.next() ?? "") ?? maxChars
    default: paths.append(argument)
    }
}

func emit(_ path: String, _ pages: Int, _ text: String, _ error: String?) {
    var object: [String: Any] = ["path": path, "pages": pages, "text": text]
    object["error"] = error ?? NSNull()
    if let data = try? JSONSerialization.data(withJSONObject: object),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

for path in paths {
    autoreleasepool {
        guard let document = PDFDocument(url: URL(fileURLWithPath: path)) else {
            emit(path, 0, "", "unreadable")
            return
        }
        if document.isLocked {
            emit(path, document.pageCount, "", "encrypted")
            return
        }
        var text = ""
        for index in 0..<min(document.pageCount, maxPages) {
            if let page = document.page(at: index), let pageText = page.string {
                text += pageText + "\n"
            }
            if text.count >= maxChars { break }
        }
        emit(path, document.pageCount, String(text.prefix(maxChars)), nil)
    }
}
