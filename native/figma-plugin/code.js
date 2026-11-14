// XENIO in Figma: agents' drawings, done in the file you have open. Every change can be undone with Figma's own Undo.
// A drawing comes as a tree, so a whole section is one step: frames with auto layout, text, shapes, colours and shadows.

figma.showUI(__html__, { width: 320, height: 280, themeColors: true });

function hex(value) {
  var s = String(value || "").trim().replace("#", "");
  if (s.length === 3) s = s.split("").map(function (c) { return c + c; }).join("");
  var n = parseInt(s.slice(0, 6), 16);
  if (isNaN(n)) throw new Error("not a colour: " + value);
  var a = s.length >= 8 ? parseInt(s.slice(6, 8), 16) / 255 : 1;
  return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255, a: a };
}

function paint(value) {
  if (!value || value === "none") return [];
  if (typeof value === "object" && value.gradient) {
    var stops = value.gradient.map(function (c, i, all) { var v = hex(c); return { position: all.length > 1 ? i / (all.length - 1) : 0, color: { r: v.r, g: v.g, b: v.b, a: v.a } }; });
    var angle = (value.angle || 90) * Math.PI / 180;
    var cos = Math.cos(angle), sin = Math.sin(angle);
    return [{ type: "GRADIENT_LINEAR", gradientStops: stops, gradientTransform: [[cos, sin, 0.5 - cos / 2 - sin / 2], [-sin, cos, 0.5 + sin / 2 - cos / 2]] }];
  }
  var v = hex(value);
  return [{ type: "SOLID", color: { r: v.r, g: v.g, b: v.b }, opacity: v.a }];
}

async function font(family, style) {
  try {
    await figma.loadFontAsync({ family: family, style: style });
    return { family: family, style: style };
  } catch (e) {
    var fallback = { family: "Inter", style: /bold/i.test(style || "") ? "Bold" : /semi/i.test(style || "") ? "Semi Bold" : /medium/i.test(style || "") ? "Medium" : "Regular" };
    await figma.loadFontAsync(fallback);
    return fallback;
  }
}

var LAYOUT = { vertical: "VERTICAL", horizontal: "HORIZONTAL" };
var ALIGN = { start: "MIN", center: "CENTER", end: "MAX", between: "SPACE_BETWEEN", baseline: "BASELINE" };
var SIZING = { fixed: "FIXED", hug: "HUG", fill: "FILL" };

function padding(node, p) {
  var v = Array.isArray(p) ? p : [p, p, p, p];
  if (v.length === 2) v = [v[0], v[1], v[0], v[1]];
  node.paddingTop = v[0] || 0; node.paddingRight = v[1] || 0; node.paddingBottom = v[2] || 0; node.paddingLeft = v[3] || 0;
}

async function style(node, spec) {
  if (spec.name) node.name = String(spec.name);
  if ("fill" in spec && "fills" in node) node.fills = paint(spec.fill);
  if (spec.stroke && "strokes" in node) { node.strokes = paint(spec.stroke); node.strokeWeight = spec.strokeWidth || 1; }
  if (spec.radius !== undefined && "cornerRadius" in node) node.cornerRadius = spec.radius;
  if (spec.opacity !== undefined) node.opacity = spec.opacity;
  if (spec.shadow) {
    var s = spec.shadow, c = hex(s.color || "#00000033");
    node.effects = [{ type: "DROP_SHADOW", color: { r: c.r, g: c.g, b: c.b, a: c.a }, offset: { x: s.x || 0, y: s.y || 4 }, radius: s.blur || 12, spread: s.spread || 0, visible: true, blendMode: "NORMAL" }];
  }
  if (spec.blur) node.effects = (node.effects || []).concat([{ type: "LAYER_BLUR", radius: spec.blur, visible: true }]);
  if (node.type === "FRAME") {
    if (spec.clip !== undefined) node.clipsContent = !!spec.clip;
    var layout = spec.layout || {};
    if (LAYOUT[layout.mode]) {
      node.layoutMode = LAYOUT[layout.mode];
      node.primaryAxisSizingMode = "AUTO";
      node.counterAxisSizingMode = "AUTO";
      if (layout.gap !== undefined) node.itemSpacing = layout.gap;
      if (layout.padding !== undefined) padding(node, layout.padding);
      if (ALIGN[layout.justify]) node.primaryAxisAlignItems = ALIGN[layout.justify];
      if (ALIGN[layout.align] && layout.align !== "between") node.counterAxisAlignItems = ALIGN[layout.align];
      if (layout.wrap) node.layoutWrap = "WRAP";
    }
  }
  if (spec.width || spec.height) {
    var w = spec.width || node.width, h = spec.height || node.height;
    if (node.type === "TEXT") { node.textAutoResize = spec.height ? "NONE" : "HEIGHT"; }
    node.resize(Math.max(1, w), Math.max(1, h));
  }
}

async function make(spec, parent) {
  var type = String(spec.type || "frame").toLowerCase();
  var node;
  if (type === "text") {
    node = figma.createText();
    node.fontName = await font(spec.font || "Inter", spec.style || "Regular");
    node.characters = String(spec.text || "");
    if (spec.size) node.fontSize = spec.size;
    if (spec.lineHeight) node.lineHeight = { value: spec.lineHeight, unit: "PERCENT" };
    if (spec.letterSpacing) node.letterSpacing = { value: spec.letterSpacing, unit: "PERCENT" };
    if (spec.align) node.textAlignHorizontal = { left: "LEFT", center: "CENTER", right: "RIGHT", justify: "JUSTIFIED" }[spec.align] || "LEFT";
    if (!("fill" in spec)) spec = Object.assign({}, spec, { fill: spec.color || "#111111" });
  } else if (type === "rect" || type === "rectangle") {
    node = figma.createRectangle();
  } else if (type === "ellipse" || type === "circle") {
    node = figma.createEllipse();
  } else if (type === "line") {
    node = figma.createLine();
  } else {
    node = figma.createFrame();
    if (!("fill" in spec)) node.fills = [];
  }
  (parent || figma.currentPage).appendChild(node);
  await style(node, spec);
  if (spec.x !== undefined) node.x = spec.x;
  if (spec.y !== undefined) node.y = spec.y;
  if (parent && parent.type === "FRAME" && parent.layoutMode !== "NONE" && spec.sizing) {
    try {
      if (SIZING[spec.sizing.w]) node.layoutSizingHorizontal = SIZING[spec.sizing.w];
      if (SIZING[spec.sizing.h]) node.layoutSizingVertical = SIZING[spec.sizing.h];
    } catch (e) { /* only inside auto layout */ }
  }
  var made = 1;
  var kids = spec.children || [];
  for (var i = 0; i < kids.length; i++) made += (await make(kids[i], node)).made;
  return { node: node, made: made };
}

function brief(node, depth) {
  var out = { id: node.id, name: node.name, type: node.type };
  if ("x" in node) { out.x = Math.round(node.x); out.y = Math.round(node.y); out.width = Math.round(node.width); out.height = Math.round(node.height); }
  if (node.type === "TEXT") out.text = node.characters.slice(0, 200);
  if (depth > 0 && "children" in node) out.children = node.children.slice(0, 60).map(function (c) { return brief(c, depth - 1); });
  return out;
}

async function find(id) {
  var node = await figma.getNodeByIdAsync(String(id || ""));
  if (!node) throw new Error("there is no node " + id + " in this file");
  return node;
}

async function run(cmd) {
  var a = cmd.args || {};
  if (cmd.action === "read") {
    if (a.id) return brief(await find(a.id), a.depth || 3);
    return { file: figma.root.name, page: figma.currentPage.name, selection: figma.currentPage.selection.map(function (n) { return brief(n, 1); }), frames: figma.currentPage.children.slice(0, 80).map(function (n) { return brief(n, 0); }) };
  }
  if (cmd.action === "build") {
    var parent = a.parent ? await find(a.parent) : null;
    var done = await make(a.tree || {}, parent);
    if (!parent && a.x === undefined && (a.tree || {}).x === undefined) {
      var right = figma.currentPage.children.filter(function (n) { return n !== done.node && "x" in n; }).reduce(function (m, n) { return Math.max(m, n.x + n.width); }, 0);
      done.node.x = right ? right + 120 : 0;
    }
    figma.currentPage.selection = [done.node];
    figma.viewport.scrollAndZoomIntoView([done.node]);
    return { id: done.node.id, name: done.node.name, nodes: done.made, width: Math.round(done.node.width), height: Math.round(done.node.height) };
  }
  if (cmd.action === "update") {
    var target = await find(a.id);
    if (target.type === "TEXT" && (a.text !== undefined || a.font)) {
      target.fontName = await font(a.font || target.fontName.family, a.style || target.fontName.style);
      if (a.text !== undefined) target.characters = String(a.text);
      if (a.size) target.fontSize = a.size;
    }
    await style(target, a);
    if (a.x !== undefined) target.x = a.x;
    if (a.y !== undefined) target.y = a.y;
    return brief(target, 0);
  }
  if (cmd.action === "delete") {
    var gone = await find(a.id);
    var name = gone.name;
    gone.remove();
    return { removed: name };
  }
  if (cmd.action === "export") {
    var node = await find(a.id);
    var bytes = await node.exportAsync({ format: "PNG", constraint: { type: "SCALE", value: Math.min(Math.max(a.scale || 1, 0.25), 2) } });
    return { png: figma.base64Encode(bytes), width: Math.round(node.width), height: Math.round(node.height) };
  }
  throw new Error("XENIO's plugin does not know " + cmd.action);
}

function where() {
  var user = "";
  try { user = figma.currentUser ? figma.currentUser.name : ""; } catch (e) { /* Figma asks for a permission to read this: not needed */ }
  figma.ui.postMessage({ type: "info", file: figma.root.name, page: figma.currentPage.name, user: user });
}

figma.on("currentpagechange", where);

figma.ui.onmessage = async function (msg) {
  if (msg.type === "load") {
    figma.ui.postMessage({ type: "token", token: (await figma.clientStorage.getAsync("xenio-token")) || "" });
    where();
  } else if (msg.type === "save") {
    await figma.clientStorage.setAsync("xenio-token", String(msg.token || ""));
  } else if (msg.type === "run") {
    try {
      figma.ui.postMessage({ type: "result", id: msg.cmd.id, ok: true, result: await run(msg.cmd) });
    } catch (e) {
      figma.ui.postMessage({ type: "result", id: msg.cmd.id, ok: false, error: String(e && e.message ? e.message : e) });
    }
  }
};
