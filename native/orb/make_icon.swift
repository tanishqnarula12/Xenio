// Draws XENIO's app icon (the Dock and Command-Tab show it while Mission Control is open): a graphite square with
// XENIO's X. Run by build.sh: `make_icon <out.png>` writes a 1024 px PNG; build.sh turns it into XENIO.icns.

import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
guard let ctx = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8, bytesPerRow: 0,
                          space: CGColorSpace(name: CGColorSpace.sRGB)!,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { exit(1) }

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

let space = CGColorSpace(name: CGColorSpace.sRGB)!
func gradient(_ colors: [CGColor], _ stops: [CGFloat]) -> CGGradient {
    CGGradient(colorsSpace: space, colors: colors as CFArray, locations: stops)!
}

// the macOS icon grid: an 824 px rounded square, centred
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
let shape = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)

ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: rgb(0x000000, 0.45))
ctx.addPath(shape)
ctx.setFillColor(rgb(0x101114))
ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(shape)
ctx.clip()
ctx.drawLinearGradient(gradient([rgb(0x17191d), rgb(0x08090b)], [0, 1]), start: CGPoint(x: 512, y: 924),
                       end: CGPoint(x: 512, y: 100), options: [])
// the X, the same drawing as docs/assets/xenio-logo.svg (its points are on a 512 grid, y pointing down)
let shapes: [[(CGFloat, CGFloat)]] = [
    [(112, 40), (222, 74), (400, 472), (290, 438)],
    [(412, 30), (286.4, 193.5), (296.7, 216.6)],
    [(100, 482), (225.6, 318.5), (215.3, 295.4)],
]
let scale: CGFloat = 1.1
ctx.setFillColor(rgb(0xf5f5f7))
for shape in shapes {
    for (i, point) in shape.enumerated() {
        let at = CGPoint(x: 512 + (point.0 - 256) * scale, y: 512 - (point.1 - 256) * scale)
        if i == 0 { ctx.move(to: at) } else { ctx.addLine(to: at) }
    }
    ctx.closePath()
}
ctx.fillPath()
ctx.restoreGState()

guard let image = ctx.makeImage(),
      let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { exit(1) }
do { try data.write(to: URL(fileURLWithPath: out)) } catch { exit(1) }
