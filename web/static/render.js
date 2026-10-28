// Renders a label design to the exact 1-bit raster the printer burns.
//
// Element boxes are in mm in design orientation; one mm is DOTS_PER_MM dots. Each element
// is drawn on its own layer and composited in order, so later elements cover earlier ones.
// QR matrices and images load asynchronously: a render made before they arrive leaves a
// gap, and onAsset() listeners are told to render again.

import {
    DOTS_PER_MM, bitsToRGBA, compositeLayer, ditherFS, grayFromRGBA, qrScale, rotateCCW,
    threshold, wrapWords,
} from "./raster.js";

const listeners = new Set();
export const onAsset = (fn) => listeners.add(fn);
const notify = () => listeners.forEach((fn) => fn());

const qrCache = new Map(); // key -> {rows} | {error} | "pending"
const imageCache = new Map(); // src -> HTMLImageElement (complete or loading)
const maskCache = new Map(); // image element key -> Uint8Array

export function qrFor(data, ecc) {
    const key = ecc + "\0" + data;
    const hit = qrCache.get(key);
    if (hit && hit !== "pending") return hit;
    if (!hit) {
        qrCache.set(key, "pending");
        fetch(`api/qr?error=${ecc}&data=${encodeURIComponent(data)}`)
            .then((r) => r.json())
            .then((j) => qrCache.set(key, j.rows ? {rows: j.rows} : {error: j.error}))
            .catch((e) => qrCache.set(key, {error: String(e)}))
            .finally(notify);
    }
    return null;
}

function imageFor(src) {
    let img = imageCache.get(src);
    if (!img) {
        img = new Image();
        img.onload = notify;
        img.src = src;
        imageCache.set(src, img);
    }
    return img.complete && img.naturalWidth ? img : null;
}

export function designSize(label) {
    const [w, h] = label.rotate ? [label.h_mm, label.w_mm] : [label.w_mm, label.h_mm];
    return [Math.round(w * DOTS_PER_MM), Math.round(h * DOTS_PER_MM)];
}

const dots = (mm) => Math.round(mm * DOTS_PER_MM);

function fontSpec(el, px) {
    return `${el.italic ? "italic " : ""}${el.bold ? "bold " : ""}${px}px ${el.font}`;
}

function layoutText(ctx, el, w, h) {
    const measure = (s) => ctx.measureText(s).width;
    const fits = (px) => {
        ctx.font = fontSpec(el, px);
        const lines = wrapWords(el.text, w, measure);
        return lines.every((l) => measure(l) <= w) && lines.length * px * 1.15 <= h;
    };
    let px = dots(el.size_mm || 0);
    if (!px) {
        let lo = 4;
        let hi = Math.max(4, h);
        while (lo < hi) {
            const mid = Math.ceil((lo + hi) / 2);
            if (fits(mid)) lo = mid;
            else hi = mid - 1;
        }
        px = lo;
    }
    ctx.font = fontSpec(el, px);
    return {px, lines: wrapWords(el.text, w, measure)};
}

function drawText(ctx, el, x, y, w, h) {
    if (el.invert) {
        ctx.fillStyle = "#000";
        ctx.fillRect(x, y, w, h);
    }
    const pad = el.invert ? Math.min(dots(0.5), w / 4) : 0;
    const {px, lines} = layoutText(ctx, el, w - 2 * pad, h - 2 * pad);
    const lineH = px * 1.15;
    const blockH = lines.length * lineH;
    let ty = y + (h - blockH) / 2;
    if (el.valign === "top") ty = y + pad;
    if (el.valign === "bottom") ty = y + h - pad - blockH;
    ctx.fillStyle = el.invert ? "#fff" : "#000";
    ctx.textBaseline = "middle";
    ctx.textAlign = el.align;
    const tx = {left: x + pad, center: x + w / 2, right: x + w - pad}[el.align];
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, h);
    ctx.clip();
    lines.forEach((line, i) => ctx.fillText(line, tx, ty + lineH * (i + 0.5)));
    ctx.restore();
}

function drawQR(ctx, el, x, y, w, h) {
    const qr = qrFor(el.data || " ", el.ecc || "m");
    if (!qr) return;
    const side = Math.min(w, h);
    if (qr.error || qrScale(qr.rows.length, side) < 2) {
        // Hatched box: too much data for this size, or the encoder refused it.
        ctx.strokeStyle = "#000";
        ctx.lineWidth = 2;
        ctx.strokeRect(x + 1, y + 1, w - 2, h - 2);
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x + w, y + h);
        ctx.moveTo(x + w, y);
        ctx.lineTo(x, y + h);
        ctx.stroke();
        return;
    }
    const n = qr.rows.length;
    const s = qrScale(n, side);
    const full = (n + 4) * s;
    const ox = x + Math.floor((w - full) / 2);
    const oy = y + Math.floor((h - full) / 2);
    ctx.fillStyle = "#fff";
    ctx.fillRect(ox, oy, full, full);
    ctx.fillStyle = "#000";
    qr.rows.forEach((row, r) => {
        for (let c = 0; c < n; c++) {
            if (row[c] === "1") ctx.fillRect(ox + (c + 2) * s, oy + (r + 2) * s, s, s);
        }
    });
}

function imageMask(el, img, w, h) {
    const key = [el.src.length, el.src.slice(-64), w, h, el.mode, el.level, el.invert].join();
    let mask = maskCache.get(key);
    if (mask) return mask;
    const c = new OffscreenCanvas(w, h);
    const g = c.getContext("2d");
    g.fillStyle = "#fff";
    g.fillRect(0, 0, w, h);
    const k = Math.min(w / img.naturalWidth, h / img.naturalHeight);
    const iw = img.naturalWidth * k;
    const ih = img.naturalHeight * k;
    g.drawImage(img, (w - iw) / 2, (h - ih) / 2, iw, ih);
    let gray = grayFromRGBA(g.getImageData(0, 0, w, h).data, w, h);
    if (el.invert) gray = gray.map((v) => 255 - v);
    mask = el.mode === "threshold" ? threshold(gray, w, h, el.level) : ditherFS(gray, w, h);
    if (maskCache.size > 64) maskCache.clear();
    maskCache.set(key, mask);
    return mask;
}

function drawImageEl(ctx, el, x, y, w, h) {
    const img = imageFor(el.src);
    if (!img || w < 1 || h < 1) return;
    const mask = imageMask(el, img, w, h);
    const data = ctx.createImageData(w, h);
    for (let i = 0; i < mask.length; i++) {
        if (mask[i]) data.data[4 * i + 3] = 255; // black where burned, clear elsewhere
    }
    ctx.putImageData(data, x, y);
}

function drawRect(ctx, el, x, y, w, h) {
    const r = Math.min(dots(el.radius_mm || 0), w / 2, h / 2);
    const lw = Math.max(1, dots(el.stroke_mm || 0));
    ctx.fillStyle = "#000";
    ctx.strokeStyle = "#000";
    ctx.beginPath();
    if (el.fill) {
        ctx.roundRect(x, y, w, h, r);
        ctx.fill();
    } else {
        ctx.lineWidth = lw;
        ctx.roundRect(x + lw / 2, y + lw / 2, w - lw, h - lw, Math.max(0, r - lw / 2));
        ctx.stroke();
    }
}

const DRAW = {text: drawText, qr: drawQR, image: drawImageEl, rect: drawRect};

// Label -> {bits, w, h} in design orientation.
export function renderBits(label) {
    const [W, H] = designSize(label);
    const bits = new Uint8Array(W * H);
    const layer = new OffscreenCanvas(Math.max(1, W), Math.max(1, H));
    const ctx = layer.getContext("2d", {willReadFrequently: true});
    for (const el of label.elements) {
        ctx.clearRect(0, 0, W, H);
        const x = dots(el.x);
        const y = dots(el.y);
        const w = Math.max(1, dots(el.w));
        const h = Math.max(1, dots(el.h));
        DRAW[el.type](ctx, el, x, y, w, h);
        compositeLayer(bits, ctx.getImageData(0, 0, W, H).data, W, H);
    }
    return {bits, w: W, h: H};
}

// Draw a rendered label onto a visible canvas.
export function paint(canvas, {bits, w, h}, ink, paper) {
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    const data = ctx.createImageData(w, h);
    bitsToRGBA(bits, data.data, ink, paper);
    ctx.putImageData(data, 0, 0);
}

// PNG data URL in printer orientation (x across the head), as /api/print expects.
export async function printPNG(label) {
    let {bits, w, h} = renderBits(label);
    if (label.rotate) {
        bits = rotateCCW(bits, w, h);
        [w, h] = [h, w];
    }
    const c = document.createElement("canvas");
    paint(c, {bits, w, h});
    return c.toDataURL("image/png");
}

function missing(label) {
    return label.elements.some((el) =>
        (el.type === "qr" && !qrFor(el.data || " ", el.ecc || "m")) ||
        (el.type === "image" && !imageFor(el.src)));
}

// Resolves once every QR matrix and image the label uses has loaded.
export async function whenReady(label, timeoutMs = 8000) {
    const end = Date.now() + timeoutMs;
    while (missing(label)) {
        if (Date.now() > end) throw new Error(`"${label.name}": QR codes or images did not load`);
        await new Promise((r) => setTimeout(r, 50));
    }
}
