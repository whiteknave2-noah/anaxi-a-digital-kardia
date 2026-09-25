#!/usr/bin/swift
import AppKit
import Foundation
import PDFKit

func fail(_ message: String, _ code: Int32 = 2) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

guard CommandLine.arguments.count == 5 else {
    fail("usage: pdf_page_renderer.swift INPUT.pdf PAGE_INDEX OUTPUT.png MAX_LONG_SIDE")
}

let input = CommandLine.arguments[1]
guard let pageIndex = Int(CommandLine.arguments[2]), pageIndex >= 0 else {
    fail("page index must be a non-negative integer")
}
let output = CommandLine.arguments[3]
guard let maxLongSide = Int(CommandLine.arguments[4]), maxLongSide > 0 else {
    fail("max long side must be a positive integer")
}

guard let document = PDFDocument(url: URL(fileURLWithPath: input)) else {
    fail("PDF_MALFORMED")
}
guard pageIndex < document.pageCount, let page = document.page(at: pageIndex) else {
    fail("PDF_PAGE_OUT_OF_RANGE")
}

let bounds = page.bounds(for: .mediaBox)
guard bounds.width > 0, bounds.height > 0 else {
    fail("PDF_MALFORMED")
}
let scale = min(Double(maxLongSide) / bounds.width, Double(maxLongSide) / bounds.height)
let target = NSSize(
    width: max(1.0, floor(bounds.width * scale)),
    height: max(1.0, floor(bounds.height * scale))
)
let image = page.thumbnail(of: target, for: .mediaBox)
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else {
    fail("PDF_RENDER_FAILED")
}

do {
    try png.write(to: URL(fileURLWithPath: output), options: .atomic)
} catch {
    fail("PDF_RENDER_FAILED: \(error)")
}
