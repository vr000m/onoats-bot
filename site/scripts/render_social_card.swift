#!/usr/bin/env swift

import AppKit
import Foundation

let script = URL(fileURLWithPath: #filePath)
let siteRoot = script.deletingLastPathComponent().deletingLastPathComponent()
let repoRoot = siteRoot.deletingLastPathComponent()
let markURL = repoRoot.appendingPathComponent("assets/brand/onoats-mark.svg")
let outputURL = siteRoot.appendingPathComponent("public/img/onoats-social-1.6.0.png")
let checking = CommandLine.arguments.dropFirst().contains("--check")

let width: CGFloat = 1200
let height: CGFloat = 630

func color(_ red: Int, _ green: Int, _ blue: Int, alpha: CGFloat = 1) -> NSColor {
    NSColor(
        calibratedRed: CGFloat(red) / 255,
        green: CGFloat(green) / 255,
        blue: CGFloat(blue) / 255,
        alpha: alpha
    )
}

func drawText(
    _ text: String,
    in rect: NSRect,
    font: NSFont,
    foreground: NSColor,
    alignment: NSTextAlignment = .left
) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = alignment
    paragraph.lineBreakMode = .byWordWrapping
    text.draw(
        in: rect,
        withAttributes: [
            .font: font,
            .foregroundColor: foreground,
            .paragraphStyle: paragraph,
        ]
    )
}

func drawPill(_ text: String, x: CGFloat, width: CGFloat) {
    let rect = NSRect(x: x, y: 60, width: width, height: 38)
    let path = NSBezierPath(roundedRect: rect, xRadius: 19, yRadius: 19)
    color(255, 248, 232, alpha: 0.18).setStroke()
    path.lineWidth = 1
    path.stroke()
    drawText(
        text,
        in: NSRect(x: x + 10, y: 69, width: width - 20, height: 20),
        font: NSFont.monospacedSystemFont(ofSize: 14, weight: .bold),
        foreground: color(255, 248, 232),
        alignment: .center
    )
}

// Draw into a bitmap of exactly 1200x630 pixels. An NSImage with lockFocus renders at
// the display's backing scale, which gives 2400x1260 on a Retina Mac.
guard
    let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: Int(width),
        pixelsHigh: Int(height),
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ),
    let context = NSGraphicsContext(bitmapImageRep: bitmap)
else {
    fputs("could not create drawing context\n", stderr)
    exit(1)
}
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
context.imageInterpolation = .high

let background = NSGradient(
    starting: color(39, 48, 63),
    ending: color(23, 43, 78)
)!
background.draw(in: NSRect(x: 0, y: 0, width: width, height: height), angle: 0)

let markCircle = NSBezierPath(ovalIn: NSRect(x: 86, y: 277, width: 248, height: 248))
color(14, 28, 54, alpha: 0.75).setFill()
markCircle.fill()
color(255, 248, 232, alpha: 0.16).setStroke()
markCircle.lineWidth = 2
markCircle.stroke()

guard let mark = NSImage(contentsOf: markURL) else {
    fputs("could not load \(markURL.path)\n", stderr)
    exit(1)
}
mark.draw(
    in: NSRect(x: 106, y: 297, width: 208, height: 208),
    from: .zero,
    operation: .sourceOver,
    fraction: 1
)

drawText(
    "ONOATS  ·  LOCAL-FIRST TRANSCRIPTS",
    in: NSRect(x: 414, y: 520, width: 650, height: 26),
    font: NSFont.monospacedSystemFont(ofSize: 17, weight: .bold),
    foreground: color(241, 179, 61)
)
drawText(
    "Record both sides.",
    in: NSRect(x: 412, y: 427, width: 720, height: 82),
    font: NSFont.systemFont(ofSize: 64, weight: .bold),
    foreground: color(255, 248, 232)
)
drawText(
    "Keep the files.",
    in: NSRect(x: 412, y: 358, width: 720, height: 82),
    font: NSFont.systemFont(ofSize: 64, weight: .bold),
    foreground: color(241, 179, 61)
)
drawText(
    "Microphone and system audio on separate streams,\ntranscribed into plain JSONL and Markdown.",
    in: NSRect(x: 414, y: 257, width: 690, height: 72),
    font: NSFont.systemFont(ofSize: 25, weight: .medium),
    foreground: color(238, 235, 224)
)

let wave = NSBezierPath()
wave.move(to: NSPoint(x: 20, y: 86))
wave.curve(
    to: NSPoint(x: 386, y: 86),
    controlPoint1: NSPoint(x: 145, y: 86),
    controlPoint2: NSPoint(x: 182, y: 150)
)
wave.curve(
    to: NSPoint(x: 620, y: 86),
    controlPoint1: NSPoint(x: 455, y: 2),
    controlPoint2: NSPoint(x: 478, y: 188)
)
wave.curve(
    to: NSPoint(x: 920, y: 86),
    controlPoint1: NSPoint(x: 732, y: 20),
    controlPoint2: NSPoint(x: 760, y: 150)
)
wave.curve(
    to: NSPoint(x: 1180, y: 86),
    controlPoint1: NSPoint(x: 1018, y: 20),
    controlPoint2: NSPoint(x: 1034, y: 116)
)
color(241, 179, 61, alpha: 0.18).setStroke()
wave.lineWidth = 3
wave.stroke()

drawPill("ME  ·  MICROPHONE", x: 414, width: 182)
drawPill("THEM  ·  SYSTEM AUDIO", x: 610, width: 220)
drawText(
    "onoats.dev",
    in: NSRect(x: 980, y: 68, width: 150, height: 26),
    font: NSFont.systemFont(ofSize: 20, weight: .bold),
    foreground: color(255, 248, 232),
    alignment: .right
)

NSGraphicsContext.restoreGraphicsState()

guard
    let fullColor = bitmap.representation(using: .png, properties: [.compressionFactor: 0.9])
else {
    fputs("could not encode social card\n", stderr)
    exit(1)
}

/// The full-colour render is about 700 KB, too heavy for link previews (WhatsApp
/// skips images over roughly 300 KB). Quantizing with pngquant brings it to
/// about 40 KB with no visible change. `--check` compares this quantized output,
/// so it needs the same pngquant major version as the committed file: `brew install pngquant`.
/// Returns nil on failure rather than exiting: `exit` skips `defer`, which would
/// leave the scratch directory behind.
func quantized(_ data: Data) -> Data? {
    let scratch = FileManager.default.temporaryDirectory
        .appendingPathComponent("onoats-social-\(UUID().uuidString)")
    let source = scratch.appendingPathComponent("in.png")
    let result = scratch.appendingPathComponent("out.png")
    defer { try? FileManager.default.removeItem(at: scratch) }
    do {
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        try data.write(to: source)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["pngquant", "--quality=80-95", "--strip", "--output", result.path, source.path]
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0, let out = try? Data(contentsOf: result) else {
            fputs("pngquant failed (exit \(process.terminationStatus)); install it with: brew install pngquant\n", stderr)
            return nil
        }
        return out
    } catch {
        fputs("could not run pngquant: \(error)\n", stderr)
        return nil
    }
}

guard let png = quantized(fullColor) else { exit(1) }

if checking {
    guard let current = try? Data(contentsOf: outputURL), current == png else {
        fputs("social card is stale; run swift scripts/render_social_card.swift\n", stderr)
        exit(1)
    }
    print("social card: ok")
} else {
    do {
        try png.write(to: outputURL, options: .atomic)
        print("updated \(outputURL.path)")
    } catch {
        fputs("could not write social card: \(error)\n", stderr)
        exit(1)
    }
}
