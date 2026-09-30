// OCR of images and scanned PDFs with the Vision framework.
//
// Usage: ocr [--level fast|accurate] [--max-pages N] [--max-chars N] file...
// Prints one JSON object per file: {"path", "pages", "text", "error"}.
// PDFs are rendered page by page (at most --max-pages) before recognition.
import Foundation
import PDFKit
import Vision
import ImageIO
import AppKit

var accurate = true
var maxPages = 5
var maxChars = 100_000
var paths: [String] = []
var arguments = CommandLine.arguments.dropFirst().makeIterator()
while let argument = arguments.next() {
    switch argument {
    case "--level": accurate = (arguments.next() ?? "accurate") != "fast"
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

func recognize(_ image: CGImage) -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = accurate ? .accurate : .fast
    request.recognitionLanguages = ["fr-FR", "en-US"]
    request.usesLanguageCorrection = accurate
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    guard (try? handler.perform([request])) != nil else { return "" }
    let observations = request.results ?? []
    return observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

/// Renders a PDF page at roughly 200 dpi, capped to 3000 px on the long side.
func render(_ page: PDFPage) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    guard box.width > 0, box.height > 0 else { return nil }
    let scale = min(200.0 / 72.0, 3000.0 / max(box.width, box.height))
    let width = Int(box.width * scale), height = Int(box.height * scale)
    guard let context = CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { return nil }
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    return context.makeImage()
}

for path in paths {
    autoreleasepool {
        let url = URL(fileURLWithPath: path)
        if path.lowercased().hasSuffix(".pdf") {
            guard let document = PDFDocument(url: url) else { emit(path, 0, "", "unreadable"); return }
            if document.isLocked { emit(path, document.pageCount, "", "encrypted"); return }
            var text = ""
            for index in 0..<min(document.pageCount, maxPages) {
                autoreleasepool {
                    if let page = document.page(at: index), let image = render(page) {
                        text += recognize(image) + "\n"
                    }
                }
                if text.count >= maxChars { break }
            }
            emit(path, document.pageCount, String(text.prefix(maxChars)), nil)
        } else {
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
                emit(path, 0, "", "unreadable")
                return
            }
            emit(path, 1, String(recognize(image).prefix(maxChars)), nil)
        }
    }
}
