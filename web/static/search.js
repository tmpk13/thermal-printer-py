// Fuzzy search over Unicode character names.
//
// Every query word must match a name: whole word > word prefix > substring > letters in
// order (subsequence, which also forgives dropped letters). Common words that Unicode
// spells differently are expanded through SYNONYMS.

export const SYNONYMS = {
    TICK: ["CHECK MARK"],
    CHECK: ["CHECK MARK"],
    CHECKBOX: ["BALLOT BOX"],
    CROSS: ["BALLOT X", "MULTIPLICATION X", "CROSS"],
    DEG: ["DEGREE"],
    OHM: ["OHM", "OMEGA"],
    MICRO: ["MICRO"],
    UM: ["MICRO"],
    PLUSMINUS: ["PLUS-MINUS"],
    TM: ["TRADE MARK"],
    TRADEMARK: ["TRADE MARK"],
    WARN: ["WARNING"],
    CAUTION: ["WARNING"],
    PHONE: ["TELEPHONE"],
    MAIL: ["ENVELOPE"],
    EMAIL: ["ENVELOPE"],
    BOLT: ["HIGH VOLTAGE", "LIGHTNING"],
    ZAP: ["HIGH VOLTAGE"],
    RECYCLE: ["RECYCLING"],
    SNOW: ["SNOWFLAKE"],
    SMILE: ["SMILING"],
    HAPPY: ["SMILING"],
    SAD: ["FROWNING"],
    DOT: ["BULLET", "DOT"],
    LEFT: ["LEFTWARDS"],
    RIGHT: ["RIGHTWARDS"],
    UP: ["UPWARDS"],
    DOWN: ["DOWNWARDS"],
    INFINITY: ["INFINITY"],
    SQRT: ["SQUARE ROOT"],
    PI: ["PI"],
    EURO: ["EURO SIGN"],
    POUND: ["POUND SIGN"],
    YEN: ["YEN SIGN"],
    MALE: ["MALE SIGN"],
    FEMALE: ["FEMALE SIGN"],
    HOT: ["HOT", "FIRE"],
    COLD: ["SNOWFLAKE", "COLD"],
    GND: ["GROUND"],
};

// chars: [[codepoint, NAME], ...] as served by /api/unicode.
export function buildIndex(chars) {
    return {
        cps: chars.map((c) => c[0]),
        names: chars.map((c) => c[1]),
        padded: chars.map((c) => " " + c[1].replace(/-/g, " ") + " "),
    };
}

// Letters in order, starting at a word and spanning at most about twice the token, so
// "arow" finds ARROW but "tick" does not find T..I..C..K scattered over a long name.
function subsequenceScore(padded, token) {
    const maxSpan = token.length * 2;
    for (let s = padded.indexOf(" " + token[0]); s >= 0; s = padded.indexOf(" " + token[0], s + 1)) {
        let j = 0;
        let i = s + 1;
        for (; i < padded.length && i - s <= maxSpan && j < token.length; i++) {
            if (padded[i] === token[j]) j++;
        }
        if (j === token.length) return 0.5;
    }
    return 0;
}

// Score of one alternative spelling of a query word against one name (0 = no match).
function altScore(name, padded, alt) {
    if (alt.includes(" ")) return name.includes(alt) ? 3 : 0;
    if (padded.includes(" " + alt + " ")) return 3;
    if (padded.includes(" " + alt)) return 2;
    if (name.includes(alt)) return 1;
    return alt.length >= 3 ? subsequenceScore(padded, alt) : 0;
}

function alternatives(token) {
    return [token, ...(SYNONYMS[token] || [])];
}

function directHit(query) {
    const hex = query.match(/^(?:U\+|0X)?([0-9A-F]{2,6})$/i);
    const cps = [];
    if (hex && /^(U\+|0X)/i.test(query)) cps.push(parseInt(hex[1], 16));
    else if (hex && hex[1].length >= 4) cps.push(parseInt(hex[1], 16));
    // A pasted character (or a few) finds itself.
    for (const ch of query) if (ch.codePointAt(0) > 0x7e) cps.push(ch.codePointAt(0));
    return cps;
}

export function search(index, query, limit = 240) {
    query = query.trim();
    if (!query) return [];
    const direct = directHit(query);
    const tokens = query.toUpperCase().split(/[\s-]+/).filter(Boolean).map(alternatives);
    const scored = [];
    const n = index.names.length;
    for (let i = 0; i < n; i++) {
        const name = index.names[i];
        const padded = index.padded[i];
        let total = 0;
        for (const alts of tokens) {
            let best = 0;
            for (const alt of alts) best = Math.max(best, altScore(name, padded, alt));
            if (!best) {
                total = 0;
                break;
            }
            total += best;
        }
        if (direct.includes(index.cps[i])) total += 100;
        if (total) scored.push([total - name.length / 1000, i]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    return scored.slice(0, limit).map(([, i]) => ({cp: index.cps[i], name: index.names[i]}));
}

// Most-used bar: characters by use count, topped up with defaults not already shown.
export function mostUsed(counts, defaults, size = 20) {
    const used = Object.entries(counts)
        .filter(([, c]) => c > 0)
        .sort((a, b) => b[1] - a[1] || a[0] - b[0])
        .map(([cp]) => Number(cp));
    const out = used.slice(0, size);
    for (const cp of defaults) {
        if (out.length >= size) break;
        if (!out.includes(cp)) out.push(cp);
    }
    return out;
}
