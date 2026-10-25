// Draws XENIO's app icon (the Dock and Command-Tab show it while Mission Control is open): a graphite square with
// XENIO's xenon orb. Run by build.sh: `make_icon <out.png>` writes a 1024 px PNG; build.sh turns it into XENIO.icns.

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
ctx.drawLinearGradient(gradient([rgb(0x22252b), rgb(0x0b0c0e)], [0, 1]), start: CGPoint(x: 512, y: 924),
                       end: CGPoint(x: 512, y: 100), options: [])
let centre = CGPoint(x: 512, y: 512)
let radius: CGFloat = 268
// the glow the orb casts on the graphite
ctx.drawRadialGradient(gradient([rgb(0xa9c8ff, 0.26), rgb(0xa9c8ff, 0.06), rgb(0xa9c8ff, 0)], [0, 0.55, 1]),
                       startCenter: centre, startRadius: 0, endCenter: centre, endRadius: 420, options: [])
// XENIO's orb as the app draws it: a sphere of fine points (a Fibonacci lattice), the near side brighter, lit from
// the upper left; the far side shows faintly through
let count = 1700
let golden = Double.pi * (3 - 5.squareRoot())
let light = (x: -0.45, y: 0.55, z: 0.70)
var dots: [(x: CGFloat, y: CGFloat, z: Double, lit: Double)] = []
for i in 0..<count {
    let y = 1 - (Double(i) + 0.5) / Double(count) * 2
    let r = (1 - y * y).squareRoot()
    let theta = golden * Double(i)
    let x = cos(theta) * r, z = sin(theta) * r
    // a slight tilt so the lattice does not look like stacked rings
    let tilt = 0.42
    let yt = y * cos(tilt) - z * sin(tilt), zt = y * sin(tilt) + z * cos(tilt)
    let lit = max(0, x * light.x + yt * light.y + zt * light.z)
    dots.append((centre.x + CGFloat(x) * radius, centre.y + CGFloat(yt) * radius, zt, lit))
}
for d in dots.sorted(by: { $0.z < $1.z }) {
    let near = (d.z + 1) / 2  // 0 far .. 1 near
    let size = CGFloat(3.0 + 5.5 * near)
    let alpha = CGFloat(0.10 + 0.55 * near * near + 0.35 * d.lit * near)
    let tone = d.lit > 0.55 && near > 0.6 ? rgb(0xf2f6ff, min(1, alpha)) : rgb(0xa9c8ff, min(1, alpha))
    ctx.setFillColor(tone)
    ctx.fillEllipse(in: CGRect(x: d.x - size / 2, y: d.y - size / 2, width: size, height: size))
}
// a soft core of light, as in the app's idle orb
ctx.drawRadialGradient(gradient([rgb(0xdbe8ff, 0.22), rgb(0xa9c8ff, 0.0)], [0, 1]),
                       startCenter: CGPoint(x: centre.x - 60, y: centre.y + 70), startRadius: 0,
                       endCenter: centre, endRadius: radius * 0.95, options: [])
ctx.restoreGState()

// a hairline edge, as on Apple's own dark icons
ctx.addPath(shape)
ctx.setStrokeColor(rgb(0xffffff, 0.08))
ctx.setLineWidth(3)
ctx.strokePath()

guard let image = ctx.makeImage(),
      let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: out))
