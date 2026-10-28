import {HEAD_DOTS, DOTS_PER_MM} from "./raster.js";
import {onAsset, paint, printPNG, renderBits, rotateElement, whenReady} from "./render.js";
import {buildIndex, mostUsed, search} from "./search.js";

const $ = (sel, root = document) => root.querySelector(sel);
const MAX_W_MM = HEAD_DOTS / DOTS_PER_MM;
const SNAP_MM = 0.5;
const STORE = "tprint.batch";
const USE_STORE = "tprint.unicodeUse";
// Shown in the most-used bar until real use fills it: check, ballot X, degree, ohm, micro,
// plus-minus, warning, high voltage, arrows, bullet, star, heart, ballot boxes, recycling.
const DEFAULT_CHARS = [0x2713, 0x2717, 0xb0, 0x2126, 0xb5, 0xb1, 0x26a0, 0x26a1, 0x2190,
    0x2191, 0x2192, 0x2193, 0x2022, 0x2605, 0x2665, 0x2610, 0x2611, 0x2612, 0x267b, 0x2116];

const store = {
    get(key, fallback) {
        try {
            return JSON.parse(localStorage.getItem(key)) ?? fallback;
        } catch {
            return fallback;
        }
    },
    set(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
            return true;
        } catch {
            return false;
        }
    },
};

let printerMm = null; // [w, h] of the loaded label, when the printer told us
const uid = () => Math.random().toString(36).slice(2, 10);

function newLabel(name = "Label") {
    const [w, h] = printerMm || [40, 30];
    return {id: uid(), name, w_mm: w, h_mm: h, rotate: false, copies: 1, elements: []};
}

let batch = store.get(STORE, null);
if (!batch?.labels?.length) batch = {version: 1, density: 4, labels: [newLabel("Label 1")]};
let cur = 0;
let selId = null;

const label = () => batch.labels[cur];
const selected = () => label().elements.find((e) => e.id === selId) || null;
const designMm = (lb = label()) => (lb.rotate ? [lb.h_mm, lb.w_mm] : [lb.w_mm, lb.h_mm]);
const round = (v, step = 0.125) => Math.round(v / step) * step;

let saveTimer = 0;
function saveNow() {
    clearTimeout(saveTimer);
    saveTimer = 0;
    if (!store.set(STORE, batch)) setMessage("Could not save to browser storage (too big?). Export to keep it.", true);
}

function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 300);
}

// A pending debounced save would be lost if the tab closes or reloads first.
addEventListener("pagehide", () => saveTimer && saveNow());
document.addEventListener("visibilitychange", () => document.hidden && saveTimer && saveNow());

function setMessage(text, error = false) {
    const m = $("#message");
    m.textContent = text;
    m.classList.toggle("error", error);
}

// ---------- view ----------

const view = $("#view");
const overlay = $("#overlay");
const stage = $("#stage");

function renderView() {
    const lb = label();
    const out = renderBits(lb);
    paint(view, out);
    const [wmm, hmm] = designMm(lb);
    stage.style.setProperty("--aspect", `${out.w} / ${out.h}`);
    stage.style.setProperty("--ratio", String(out.w / out.h));
    overlay.replaceChildren(...lb.elements.map((el) => {
        const box = document.createElement("div");
        box.className = "box" + (el.id === selId ? " selected" : "");
        box.dataset.id = el.id;
        Object.assign(box.style, {
            left: `${(el.x / wmm) * 100}%`, top: `${(el.y / hmm) * 100}%`,
            width: `${(el.w / wmm) * 100}%`, height: `${(el.h / hmm) * 100}%`,
        });
        if (el.id === selId) {
            for (const dir of ["nw", "ne", "sw", "se"]) {
                const hd = document.createElement("div");
                hd.className = `handle ${dir}`;
                hd.dataset.dir = dir;
                box.append(hd);
            }
        }
        return box;
    }));
    const thumb = $(`.card[data-id="${lb.id}"] canvas`);
    if (thumb) paint(thumb, out);
    $("#dims").textContent = `${out.w} x ${out.h} dots`;
    syncGeometryInputs();
    save();
}

function renderAll() {
    renderBatch();
    renderLabelForm();
    renderProps();
    renderView();
}

onAsset(() => {
    renderView();
    batch.labels.forEach((lb) => {
        const t = $(`.card[data-id="${lb.id}"] canvas`);
        if (t && lb !== label()) paint(t, renderBits(lb));
    });
});

// ---------- dragging ----------

let drag = null;
overlay.addEventListener("pointerdown", (ev) => {
    // preventDefault below keeps focus where it was; release inputs so shortcuts reach the canvas.
    if (document.activeElement?.matches("input, textarea, select")) document.activeElement.blur();
    const box = ev.target.closest(".box");
    if (!box) {
        selectEl(null);
        return;
    }
    if (box.dataset.id !== selId) selectEl(box.dataset.id);
    const el = selected();
    const rect = overlay.getBoundingClientRect();
    drag = {
        dir: ev.target.dataset.dir || "move",
        start: [ev.clientX, ev.clientY],
        orig: {x: el.x, y: el.y, w: el.w, h: el.h},
        mmPerPx: designMm()[0] / rect.width,
    };
    overlay.setPointerCapture(ev.pointerId);
    ev.preventDefault();
});

overlay.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const el = selected();
    const snap = (v) => (ev.altKey ? round(v) : round(v, SNAP_MM));
    const dx = (ev.clientX - drag.start[0]) * drag.mmPerPx;
    const dy = (ev.clientY - drag.start[1]) * drag.mmPerPx;
    const o = drag.orig;
    if (drag.dir === "move") {
        el.x = snap(o.x + dx);
        el.y = snap(o.y + dy);
    } else {
        let {x, y, w, h} = o;
        if (drag.dir.includes("w")) [x, w] = [snap(o.x + dx), o.w + o.x - snap(o.x + dx)];
        if (drag.dir.includes("e")) w = snap(o.x + o.w + dx) - o.x;
        if (drag.dir.includes("n")) [y, h] = [snap(o.y + dy), o.h + o.y - snap(o.y + dy)];
        if (drag.dir.includes("s")) h = snap(o.y + o.h + dy) - o.y;
        if (w >= 1 && h >= 1) Object.assign(el, {x, y, w, h});
    }
    renderView();
});

const endDrag = () => (drag = null);
overlay.addEventListener("pointerup", endDrag);
overlay.addEventListener("pointercancel", endDrag);

function selectEl(id) {
    selId = id;
    renderProps();
    renderView();
}

// ---------- adding elements ----------

function addElement(type, extra = {}) {
    const [W, H] = designMm();
    const m = 1.5;
    const base = {id: uid(), type, x: m, y: m, w: W - 2 * m, h: H - 2 * m};
    const defaults = {
        text: {text: "Text", font: "sans-serif", bold: true, italic: false, size_mm: 0,
            align: "center", valign: "middle", invert: false},
        qr: {data: "https://example.com", ecc: "m", w: Math.min(W, H) - 2 * m, h: Math.min(W, H) - 2 * m},
        image: {src: "", mode: "dither", level: 128, invert: false},
        rect: {x: 0.5, y: 0.5, w: W - 1, h: H - 1, stroke_mm: 0.375, radius_mm: 1, fill: false},
    }[type];
    const el = {...base, ...defaults, ...extra};
    label().elements.push(el);
    selectEl(el.id);
    return el;
}

$("#add-text").onclick = () => addElement("text");
$("#add-qr").onclick = () => addElement("qr");
$("#add-rect").onclick = () => addElement("rect");
$("#add-image").onclick = () => $("#image-file").click();
$("#image-file").onchange = async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    try {
        const {src, aspect} = await loadImage(file);
        const [W, H] = designMm();
        let w = W - 2;
        let h = w / aspect;
        if (h > H - 2) [h, w] = [H - 2, (H - 2) * aspect];
        const el = selected();
        if (el?.type === "image" && ev.target.dataset.replace) {
            el.src = src;
            renderView();
        } else {
            addElement("image", {src, x: (W - w) / 2, y: (H - h) / 2, w: round(w), h: round(h)});
        }
    } catch (e) {
        setMessage(`Could not load image: ${e.message}`, true);
    } finally {
        delete ev.target.dataset.replace;
    }
};

// Downscaled grayscale copy, small enough to keep many in browser storage.
async function loadImage(file) {
    const bmp = await createImageBitmap(file);
    const maxDim = 1024;
    const k = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k);
    c.height = Math.round(bmp.height * k);
    const g = c.getContext("2d");
    g.fillStyle = "#fff";
    g.fillRect(0, 0, c.width, c.height);
    g.filter = "grayscale(1)";
    g.drawImage(bmp, 0, 0, c.width, c.height);
    return {src: c.toDataURL("image/png"), aspect: bmp.width / bmp.height};
}

// ---------- properties ----------

const FIELDS = {
    common: [["x", "X mm", "num"], ["y", "Y mm", "num"], ["w", "Width mm", "num"], ["h", "Height mm", "num"]],
    text: [
        ["text", "Text", "area"],
        ["font", "Font", "select", ["sans-serif", "serif", "monospace", "DejaVu Sans", "DejaVu Sans Mono",
            "Liberation Sans", "Noto Sans", "Noto Sans Symbols 2"]],
        ["size_mm", "Size mm (0 = fit)", "num"],
        ["align", "Align", "select", ["left", "center", "right"]],
        ["valign", "Vertical", "select", ["top", "middle", "bottom"]],
        ["bold", "Bold", "check"], ["italic", "Italic", "check"], ["invert", "White on black", "check"],
    ],
    qr: [["data", "QR data", "area"], ["ecc", "Error correction", "select", ["l", "m", "q", "h"]]],
    image: [["mode", "Mode", "select", ["dither", "threshold"]], ["level", "Threshold 0-255", "num"],
        ["invert", "Invert", "check"]],
    rect: [["stroke_mm", "Line mm", "num"], ["radius_mm", "Corner radius mm", "num"], ["fill", "Filled", "check"]],
};

function field(obj, [key, text, kind, options], onChange) {
    const wrap = document.createElement("label");
    wrap.className = `field ${kind}`;
    const span = document.createElement("span");
    span.textContent = text;
    let input;
    if (kind === "area") {
        input = document.createElement("textarea");
        input.rows = 3;
        input.value = obj[key];
    } else if (kind === "select") {
        input = document.createElement("select");
        for (const o of options) input.add(new Option(o, o));
        input.value = obj[key];
    } else {
        input = document.createElement("input");
        input.type = kind === "check" ? "checkbox" : "number";
        if (kind === "check") input.checked = obj[key];
        else {
            input.step = key === "level" ? "1" : "0.125";
            input.value = obj[key];
        }
    }
    input.dataset.key = key;
    input.addEventListener("input", () => {
        obj[key] = kind === "check" ? input.checked : kind === "num" ? Number(input.value) || 0 : input.value;
        onChange();
    });
    wrap.append(span, input);
    if (kind === "check") wrap.append(span);
    return wrap;
}

function renderProps() {
    const box = $("#props");
    const el = selected();
    if (!el) {
        box.replaceChildren(Object.assign(document.createElement("p"), {
            className: "hint", textContent: "Select an element, or add one above.",
        }));
        return;
    }
    const title = document.createElement("h3");
    title.textContent = {text: "Text", qr: "QR code", image: "Image", rect: "Box"}[el.type];
    const grid = document.createElement("div");
    grid.className = "fields";
    for (const f of [...FIELDS[el.type], ...FIELDS.common]) grid.append(field(el, f, renderView));
    const actions = document.createElement("div");
    actions.className = "row";
    const btn = (text, fn) => {
        const b = document.createElement("button");
        b.textContent = text;
        b.onclick = fn;
        actions.append(b);
        return b;
    };
    if (el.type === "image") {
        btn("Replace", () => {
            $("#image-file").dataset.replace = "1";
            $("#image-file").click();
        });
    }
    if (el.type === "text") {
        const r = btn(`Rotate (${el.rot || 0})`, () => rotateSelected(1));
        r.id = "rotate-el";
        r.title = "Turn 90 degrees clockwise (R, Shift+R for counterclockwise)";
    }
    btn("Forward", () => moveEl(1));
    btn("Back", () => moveEl(-1));
    btn("Duplicate", duplicateEl);
    btn("Delete", deleteEl).classList.add("danger");
    box.replaceChildren(title, grid, actions);
}

function syncGeometryInputs() {
    const el = selected();
    if (!el) return;
    for (const k of ["x", "y", "w", "h"]) {
        const input = $(`#props [data-key="${k}"]`);
        if (input && document.activeElement !== input) input.value = el[k];
    }
}

function moveEl(delta) {
    const els = label().elements;
    const i = els.findIndex((e) => e.id === selId);
    const j = i + delta;
    if (j < 0 || j >= els.length) return;
    [els[i], els[j]] = [els[j], els[i]];
    renderView();
}

function rotateSelected(dir) {
    const el = selected();
    if (!el) return;
    rotateElement(el, dir);
    // A box turned on a narrow label can stick out; trim it to the label so text refits.
    const [W, H] = designMm();
    const x0 = Math.max(0, el.x);
    const y0 = Math.max(0, el.y);
    el.w = Math.max(1, Math.min(el.x + el.w, W) - x0);
    el.h = Math.max(1, Math.min(el.y + el.h, H) - y0);
    [el.x, el.y] = [x0, y0];
    const b = $("#rotate-el");
    if (b) b.textContent = `Rotate (${el.rot})`;
    renderView();
}

function duplicateEl() {
    const el = selected();
    if (!el) return;
    const copy = {...el, id: uid(), x: el.x + 1, y: el.y + 1};
    label().elements.push(copy);
    selectEl(copy.id);
}

function deleteEl() {
    label().elements = label().elements.filter((e) => e.id !== selId);
    selectEl(null);
}

// ---------- label settings ----------

function renderLabelForm() {
    const lb = label();
    $("#label-name").value = lb.name;
    $("#label-w").value = lb.w_mm;
    $("#label-h").value = lb.h_mm;
    $("#label-rotate").checked = lb.rotate;
    $("#use-printer").disabled = !printerMm;
}

$("#label-name").oninput = (ev) => {
    label().name = ev.target.value;
    $(`.card[data-id="${label().id}"] .name`).textContent = label().name;
    save();
};
$("#label-w").oninput = (ev) => {
    label().w_mm = Math.min(MAX_W_MM, Math.max(1, Number(ev.target.value) || 1));
    renderView();
};
$("#label-h").oninput = (ev) => {
    label().h_mm = Math.max(1, Number(ev.target.value) || 1);
    renderView();
};
$("#label-rotate").onchange = (ev) => {
    label().rotate = ev.target.checked;
    // Transpose boxes so they stay on the label; content itself stays upright.
    for (const el of label().elements) [el.x, el.y, el.w, el.h] = [el.y, el.x, el.h, el.w];
    renderView();
};
$("#use-printer").onclick = () => {
    if (!printerMm) return;
    [label().w_mm, label().h_mm] = printerMm;
    renderLabelForm();
    renderView();
};

// ---------- batch ----------

function renderBatch() {
    const list = $("#batch");
    list.replaceChildren(...batch.labels.map((lb, i) => {
        const card = document.createElement("li");
        card.className = "card" + (i === cur ? " current" : "");
        card.dataset.id = lb.id;
        card.innerHTML = `<canvas></canvas><div class="meta"><span class="name"></span>
            <label class="qty">x <input type="number" min="0" step="1"></label>
            <span class="row"><button data-act="dup" title="Duplicate label">Copy</button>
            <button data-act="del" class="danger" title="Delete label">Del</button></span></div>`;
        $(".name", card).textContent = lb.name;
        const qty = $("input", card);
        qty.value = lb.copies;
        qty.oninput = () => {
            lb.copies = Math.max(0, Math.floor(Number(qty.value) || 0));
            updateTotal();
            save();
        };
        card.onclick = (ev) => {
            const act = ev.target.dataset.act;
            if (act === "dup") {
                const copy = structuredClone(lb);
                copy.id = uid();
                copy.name = lb.name + " copy";
                copy.elements.forEach((e) => (e.id = uid()));
                batch.labels.splice(i + 1, 0, copy);
                cur = i + 1;
            } else if (act === "del") {
                if (batch.labels.length === 1) return;
                batch.labels.splice(i, 1);
                cur = Math.min(cur, batch.labels.length - 1);
            } else if (ev.target.closest("input")) {
                return;
            } else {
                cur = i;
            }
            selId = null;
            renderAll();
        };
        paint($("canvas", card), renderBits(lb));
        return card;
    }));
    updateTotal();
}

function updateTotal() {
    const n = batch.labels.reduce((s, lb) => s + lb.copies, 0);
    $("#total").textContent = `${n} label${n === 1 ? "" : "s"}`;
}

$("#new-label").onclick = () => {
    batch.labels.push(newLabel(`Label ${batch.labels.length + 1}`));
    cur = batch.labels.length - 1;
    selId = null;
    renderAll();
};

$("#export").onclick = () => {
    const blob = new Blob([JSON.stringify(batch, null, 1)], {type: "application/json"});
    const a = Object.assign(document.createElement("a"), {
        href: URL.createObjectURL(blob), download: "labels.json",
    });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};
$("#import").onclick = () => $("#import-file").click();
$("#import-file").onchange = async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    try {
        const b = JSON.parse(await file.text());
        if (!Array.isArray(b.labels) || !b.labels.length) throw new Error("no labels in file");
        batch = {version: 1, density: 4, ...b};
        cur = 0;
        selId = null;
        $("#density").value = batch.density;
        renderAll();
        setMessage(`Loaded ${batch.labels.length} labels`);
    } catch (e) {
        setMessage(`Import failed: ${e.message}`, true);
    }
};

// ---------- printer ----------

async function refreshPrinter() {
    const chip = $("#printer");
    chip.textContent = "Checking printer...";
    try {
        const r = await fetch("api/printer");
        const j = await r.json();
        if (!r.ok) throw new Error(j.error);
        printerMm = j.label_mm;
        chip.textContent = printerMm ? `Printer ready, ${printerMm[0]} x ${printerMm[1]} mm label` : "Printer ready";
        chip.className = "chip ok";
    } catch (e) {
        printerMm = null;
        chip.textContent = `No printer: ${e.message}`;
        chip.className = "chip bad";
    }
    renderLabelForm();
}
$("#printer").onclick = refreshPrinter;

for (let d = 0; d <= 15; d++) $("#density").add(new Option(String(d), String(d)));
$("#density").value = batch.density;
$("#density").onchange = (ev) => {
    batch.density = Number(ev.target.value);
    save();
};

async function printLabels(labels) {
    labels = labels.filter((lb) => lb.copies > 0);
    if (!labels.length) {
        setMessage("Nothing to print: every quantity is 0", true);
        return;
    }
    setMessage("Preparing...");
    try {
        const items = [];
        for (const lb of labels) {
            await whenReady(lb);
            items.push({png: await printPNG(lb), copies: lb.copies});
        }
        const r = await fetch("api/print", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({density: batch.density, labels: items}),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error);
        pollJob();
    } catch (e) {
        setMessage(`Print failed: ${e.message}`, true);
    }
}

let polling = false;
async function pollJob() {
    if (polling) return;
    polling = true;
    $("#cancel").hidden = false;
    try {
        for (;;) {
            const j = await (await fetch("api/job")).json();
            if (j.running) {
                setMessage(`Printing ${j.done + 1} of ${j.total}...`);
            } else {
                if (j.error) setMessage(`Printed ${j.done} of ${j.total}: ${j.error}`, true);
                else setMessage(`Printed ${j.done} label${j.done === 1 ? "" : "s"}`);
                break;
            }
            await new Promise((r) => setTimeout(r, 500));
        }
    } catch (e) {
        setMessage(`Lost contact with the server: ${e.message}`, true);
    } finally {
        polling = false;
        $("#cancel").hidden = true;
    }
}

$("#print-batch").onclick = () => printLabels(batch.labels);
$("#print-one").onclick = () => printLabels([label()]);
$("#cancel").onclick = () => fetch("api/job/cancel", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});

// ---------- unicode panel ----------

let uniIndex = null;
let useCounts = store.get(USE_STORE, {});
let lastText = null; // {input, start, end} of the last focused text area

document.addEventListener("focusout", (ev) => {
    if (ev.target.matches?.("#props textarea")) {
        lastText = {input: ev.target, start: ev.target.selectionStart, end: ev.target.selectionEnd};
    }
});

function charButton(cp, name) {
    const b = document.createElement("button");
    b.className = "glyph";
    b.textContent = String.fromCodePoint(cp);
    b.title = `${name || ""} U+${cp.toString(16).toUpperCase().padStart(4, "0")}`.trim();
    b.onmousedown = (ev) => ev.preventDefault(); // keep the caret in the text box
    b.onclick = () => insertChar(cp);
    b.onmouseenter = () => ($("#uni-name").textContent = b.title);
    return b;
}

function nameOf(cp) {
    if (!uniIndex) return "";
    const i = uniIndex.cps.indexOf(cp);
    return i < 0 ? "" : uniIndex.names[i];
}

function renderMostUsed() {
    $("#most-used").replaceChildren(...mostUsed(useCounts, DEFAULT_CHARS).map((cp) => charButton(cp, nameOf(cp))));
}

function insertChar(cp) {
    const ch = String.fromCodePoint(cp);
    const el = selected();
    const area = $("#props textarea[data-key='text'], #props textarea[data-key='data']");
    if (el && area) {
        const focused = document.activeElement === area;
        const start = focused ? area.selectionStart : lastText?.input === area ? lastText.start : area.value.length;
        const end = focused ? area.selectionEnd : lastText?.input === area ? lastText.end : area.value.length;
        area.setRangeText(ch, start, end, "end");
        area.dispatchEvent(new Event("input"));
        area.focus();
    } else {
        addElement("text", {text: ch});
    }
    useCounts[cp] = (useCounts[cp] || 0) + 1;
    store.set(USE_STORE, useCounts);
    renderMostUsed();
}

let searchTimer = 0;
$("#uni-search").oninput = (ev) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(ev.target.value), 120);
};

function runSearch(q) {
    const grid = $("#uni-results");
    if (!uniIndex) {
        grid.textContent = "Loading character names...";
        return;
    }
    const hits = search(uniIndex, q);
    grid.replaceChildren(...hits.map((h) => charButton(h.cp, h.name)));
    $("#uni-name").textContent = q.trim() ? `${hits.length}${hits.length === 240 ? "+" : ""} matches` : "";
}

async function loadUnicode() {
    try {
        const j = await (await fetch("api/unicode")).json();
        uniIndex = buildIndex(j.chars);
        $("#uni-search").placeholder = `Search ${j.chars.length} characters (Unicode ${j.version})`;
        renderMostUsed();
        if ($("#uni-search").value) runSearch($("#uni-search").value);
    } catch (e) {
        $("#uni-name").textContent = `Character names unavailable: ${e.message}`;
    }
}

// ---------- keyboard ----------

document.addEventListener("keydown", (ev) => {
    if (ev.target.closest("input, textarea, select")) return;
    const el = selected();
    if (ev.key === "Escape") return selectEl(null);
    if (!el) return;
    const step = ev.shiftKey ? 1 / DOTS_PER_MM : SNAP_MM;
    const moves = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]};
    if (moves[ev.key]) {
        el.x = round(el.x + moves[ev.key][0]);
        el.y = round(el.y + moves[ev.key][1]);
        renderView();
    } else if (ev.key === "Delete" || ev.key === "Backspace") {
        deleteEl();
    } else if (ev.key === "d" && (ev.ctrlKey || ev.metaKey)) {
        duplicateEl();
    } else if (ev.key.toLowerCase() === "r" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        if (el.type !== "text") return;
        rotateSelected(ev.shiftKey ? -1 : 1);
    } else {
        return;
    }
    ev.preventDefault();
});

// ---------- theme ----------

const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    $("#theme").textContent = `Theme: ${t}`;
}
let theme = store.get("tprint.theme", "system");
applyTheme(theme);
$("#theme").onclick = () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    store.set("tprint.theme", theme);
    applyTheme(theme);
};

// ---------- start ----------

renderAll();
renderMostUsed();
refreshPrinter();
loadUnicode();
fetch("api/job").then((r) => r.json()).then((j) => j.running && pollJob()).catch(() => {});
