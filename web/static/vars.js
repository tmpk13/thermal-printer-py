// Substitution variables for text and QR data.
//
// A label's variable table is CSV, or tab-separated text pasted from a spreadsheet: the first
// line names the columns and every later line prints one label, so {name} in an element's
// text or QR data becomes that row's value. Built-ins are always defined: {n} counts every
// label printed from the design, starting at the label's start number; {copy} and {row} are
// 1-based; {total} is how many labels the design prints; {date} and {time} are local time.
// {n:03} pads a value with zeros to three characters. Unknown names stay as written, so
// literal braces (JSON, code) print unchanged.

const PLACEHOLDER = /\{([^{}:\n]+)(?::(\d+))?\}/g;
const MAX_PAD = 64;
export const BUILTINS = ["n", "copy", "row", "total", "date", "time"];

// Records of cells. Quoted cells may hold the separator, newlines and doubled quotes.
function records(src, sep) {
    const out = [];
    let rec = [];
    let cell = "";
    let quoted = false;
    for (let i = 0; i < src.length; i++) {
        const ch = src[i];
        if (quoted) {
            if (ch === '"' && src[i + 1] === '"') {
                cell += '"';
                i++;
            } else if (ch === '"') {
                quoted = false;
            } else {
                cell += ch;
            }
        } else if (ch === '"' && cell === "") {
            quoted = true;
        } else if (ch === sep) {
            rec.push(cell);
            cell = "";
        } else if (ch === "\n") {
            rec.push(cell);
            out.push(rec);
            rec = [];
            cell = "";
        } else {
            cell += ch;
        }
    }
    rec.push(cell);
    out.push(rec);
    return out;
}

// Text -> {names, rows}; rows are {name: value} objects. Tab-separated when the header line
// has a tab, else comma-separated. Blank lines are skipped and short rows read as "".
export function parseTable(text) {
    const src = (text || "").replace(/\r\n?/g, "\n");
    const sep = src.split("\n", 1)[0].includes("\t") ? "\t" : ",";
    const [head, ...body] = records(src, sep).map((r) => r.map((c) => c.trim()));
    const rows = body
        .filter((r) => r.some((c) => c))
        .map((r) => Object.fromEntries(head.map((name, i) => [name, r[i] ?? ""]).filter(([name]) => name)));
    return {names: [...new Set(head.filter(Boolean))], rows};
}

export function fill(template, vars) {
    return (template || "").replace(PLACEHOLDER, (whole, name, width) => {
        const v = vars[name.trim()];
        if (v === undefined) return whole;
        return width ? String(v).padStart(Math.min(Number(width), MAX_PAD), "0") : String(v);
    });
}

const usedNames = (s) => [...(s || "").matchAll(PLACEHOLDER)].map((m) => m[1].trim());

const templates = (label) =>
    label.elements.flatMap((el) => (el.type === "text" ? [el.text] : el.type === "qr" ? [el.data] : []));

// The element as it prints with these values.
export function fillElement(el, vars) {
    if (el.type === "text") return {...el, text: fill(el.text, vars)};
    if (el.type === "qr") return {...el, data: fill(el.data, vars)};
    return el;
}

const rowCount = (table) => Math.max(1, table.rows.length);

// Values for one printed label: row and copy are 0-based indexes. Columns override built-ins.
export function varsFor(label, row = 0, copy = 0, now = new Date(), table = parseTable(label.vars)) {
    const start = Number.isFinite(label.start) ? label.start : 1;
    const pad2 = (v) => String(v).padStart(2, "0");
    const r = Math.min(Math.max(0, row), rowCount(table) - 1);
    return {
        n: start + r * Math.max(1, label.copies) + copy,
        copy: copy + 1,
        row: r + 1,
        total: rowCount(table) * label.copies,
        date: `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`,
        time: `${pad2(now.getHours())}:${pad2(now.getMinutes())}`,
        ...table.rows[r],
    };
}

// What printing the label produces, in order: [{vars, copies}]. One entry per row, or one
// per copy when a template uses {n} or {copy}, since then every copy differs.
export function instances(label, now = new Date()) {
    const table = parseTable(label.vars);
    const perCopy = templates(label).some((t) => usedNames(t).some((n) => n === "n" || n === "copy"));
    const out = [];
    for (let r = 0; r < rowCount(table); r++) {
        if (perCopy) {
            for (let c = 0; c < label.copies; c++) out.push({vars: varsFor(label, r, c, now, table), copies: 1});
        } else if (label.copies > 0) {
            out.push({vars: varsFor(label, r, 0, now, table), copies: label.copies});
        }
    }
    return out;
}

export const labelCount = (label) => rowCount(parseTable(label.vars)) * label.copies;
export const tableRows = (label) => parseTable(label.vars).rows.length;
