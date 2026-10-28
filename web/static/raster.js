// 1-bit raster helpers. Bitmaps are Uint8Array(w * h), 1 = burn (black), row-major.

export const DOTS_PER_MM = 8;
export const HEAD_DOTS = 384;

// Luminance 0..255 from RGBA bytes, with transparency treated as white paper.
export function grayFromRGBA(rgba, w, h) {
    const out = new Float32Array(w * h);
    for (let i = 0; i < w * h; i++) {
        const a = rgba[4 * i + 3] / 255;
        const lum = 0.299 * rgba[4 * i] + 0.587 * rgba[4 * i + 1] + 0.114 * rgba[4 * i + 2];
        out[i] = lum * a + 255 * (1 - a);
    }
    return out;
}

export function threshold(gray, w, h, level = 128) {
    const out = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) out[i] = gray[i] < level ? 1 : 0;
    return out;
}

// Floyd-Steinberg error diffusion, same kernel as Pillow's convert("1").
export function ditherFS(gray, w, h) {
    const g = Float32Array.from(gray);
    const out = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
            const i = y * w + x;
            const v = g[i];
            const black = v < 128;
            out[i] = black ? 1 : 0;
            const err = v - (black ? 0 : 255);
            if (x + 1 < w) g[i + 1] += err * 7 / 16;
            if (y + 1 < h) {
                if (x > 0) g[i + w - 1] += err * 3 / 16;
                g[i + w] += err * 5 / 16;
                if (x + 1 < w) g[i + w + 1] += err * 1 / 16;
            }
        }
    }
    return out;
}

// Paint opaque pixels of an RGBA layer into bits: dark burns, light clears, transparent keeps.
export function compositeLayer(bits, rgba, w, h) {
    for (let i = 0; i < w * h; i++) {
        if (rgba[4 * i + 3] < 128) continue;
        const lum = 0.299 * rgba[4 * i] + 0.587 * rgba[4 * i + 1] + 0.114 * rgba[4 * i + 2];
        bits[i] = lum < 128 ? 1 : 0;
    }
    return bits;
}

// Counterclockwise quarter turn (Pillow's rotate(90, expand=True)): w x h -> h x w.
export function rotateCCW(bits, w, h) {
    const out = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) out[(w - 1 - x) * h + y] = bits[y * w + x];
    }
    return out;
}

// Write bits into RGBA bytes as black/white (or the given colors) pixels.
export function bitsToRGBA(bits, rgba, ink = [0, 0, 0], paper = [255, 255, 255]) {
    for (let i = 0; i < bits.length; i++) {
        const c = bits[i] ? ink : paper;
        rgba[4 * i] = c[0];
        rgba[4 * i + 1] = c[1];
        rgba[4 * i + 2] = c[2];
        rgba[4 * i + 3] = 255;
    }
    return rgba;
}

// Whole dots per QR module so the code plus a 2-module quiet zone fits in `side` dots.
export function qrScale(modules, side) {
    return Math.floor(side / (modules + 4));
}

// Greedy word wrap of one paragraph; words wider than maxWidth get their own line.
export function wrapWords(text, maxWidth, measure) {
    const lines = [];
    for (const para of text.split("\n")) {
        const words = para.split(/ +/);
        let line = "";
        for (const word of words) {
            const next = line ? line + " " + word : word;
            if (line && measure(next) > maxWidth) {
                lines.push(line);
                line = word;
            } else {
                line = next;
            }
        }
        lines.push(line);
    }
    return lines;
}
