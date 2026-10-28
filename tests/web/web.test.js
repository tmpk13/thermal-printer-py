import {describe, expect, test} from "bun:test";
import {
    compositeLayer, ditherFS, grayFromRGBA, qrScale, rotateCCW, threshold, wrapWords,
} from "../../web/static/raster.js";
import {rotateElement} from "../../web/static/render.js";
import {buildIndex, mostUsed, search} from "../../web/static/search.js";

describe("raster", () => {
    test("rotateCCW matches Pillow rotate(90, expand=True)", () => {
        // 3x2:  a b c      ->  2x3:  c f
        //       d e f                b e
        //                            a d
        const bits = Uint8Array.from([1, 2, 3, 4, 5, 6]);
        expect([...rotateCCW(bits, 3, 2)]).toEqual([3, 6, 2, 5, 1, 4]);
    });

    test("dither keeps the mean gray level and extremes exact", () => {
        const w = 32, h = 32;
        const mid = ditherFS(new Float32Array(w * h).fill(128), w, h);
        const black = mid.reduce((s, v) => s + v, 0) / (w * h);
        expect(black).toBeGreaterThan(0.4);
        expect(black).toBeLessThan(0.6);
        expect(ditherFS(new Float32Array(16).fill(0), 4, 4).every((v) => v === 1)).toBe(true);
        expect(ditherFS(new Float32Array(16).fill(255), 4, 4).every((v) => v === 0)).toBe(true);
    });

    test("threshold and transparent pixels read as paper", () => {
        const rgba = Uint8Array.from([0, 0, 0, 255, 0, 0, 0, 0, 200, 200, 200, 255]);
        const gray = grayFromRGBA(rgba, 3, 1);
        expect([...threshold(gray, 3, 1)]).toEqual([1, 0, 0]);
    });

    test("compositeLayer burns dark, clears light, keeps transparent", () => {
        const bits = Uint8Array.from([1, 1, 0]);
        const rgba = Uint8Array.from([255, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 255]);
        expect([...compositeLayer(bits, rgba, 3, 1)]).toEqual([0, 1, 1]);
    });

    test("qrScale leaves room for the quiet zone", () => {
        expect(qrScale(21, 100)).toBe(4); // 25 modules wide
        expect(qrScale(21, 49)).toBe(1);
    });

    test("wrapWords breaks greedily and keeps explicit newlines", () => {
        const m = (s) => s.length;
        expect(wrapWords("aa bb cc", 5, m)).toEqual(["aa bb", "cc"]);
        expect(wrapWords("toolongword x", 4, m)).toEqual(["toolongword", "x"]);
        expect(wrapWords("a\nb c", 10, m)).toEqual(["a", "b c"]);
    });
});

describe("search", () => {
    const index = buildIndex([
        [0x2713, "CHECK MARK"], [0x2714, "HEAVY CHECK MARK"], [0x2611, "BALLOT BOX WITH CHECK"],
        [0x2192, "RIGHTWARDS ARROW"], [0x2190, "LEFTWARDS ARROW"], [0xb0, "DEGREE SIGN"],
        [0x2126, "OHM SIGN"], [0x3a9, "GREEK CAPITAL LETTER OMEGA"], [0x26a1, "HIGH VOLTAGE SIGN"],
        [0xb1, "PLUS-MINUS SIGN"], [0x41, "LATIN CAPITAL LETTER A"],
    ]);
    const cps = (q) => search(index, q).map((h) => h.cp);

    test("whole words rank first, shorter names break ties", () => {
        expect(cps("check")[0]).toBe(0x2713);
        expect(cps("check")).toContain(0x2611);
    });

    test("all words must match", () => {
        expect(cps("right arrow")).toEqual([0x2192]);
        expect(cps("left")).toEqual([0x2190]);
    });

    test("synonyms and dropped letters", () => {
        expect(cps("tick")[0]).toBe(0x2713);
        expect(cps("deg")).toEqual([0xb0]);
        expect(cps("bolt")).toEqual([0x26a1]);
        expect(cps("arow")).toContain(0x2192);
        expect(cps("ohm")).toEqual(expect.arrayContaining([0x2126, 0x3a9]));
        expect(cps("plusminus")).toEqual([0xb1]);
        expect(cps("chek mrk")[0]).toBe(0x2713);
    });

    test("scattered letters do not match", () => {
        const idx = buildIndex([[1, "SKIER"], [2, "TELEPHONE LOCATION SIGN WITH CROSS KEY"]]);
        expect(search(idx, "tick")).toEqual([]);
    });

    test("code points and pasted characters", () => {
        expect(cps("U+2126")[0]).toBe(0x2126);
        expect(cps("2192")[0]).toBe(0x2192);
        expect(cps(String.fromCodePoint(0xb0))[0]).toBe(0xb0);
        expect(cps("")).toEqual([]);
    });

    test("searches a full-size index quickly", () => {
        const big = buildIndex(Array.from({length: 30000}, (_, i) => [i, `SYMBOL NUMBER ${i} WORD`]));
        const t = performance.now();
        search(big, "smbl wrd");
        expect(performance.now() - t).toBeLessThan(200);
    });
});

describe("mostUsed", () => {
    test("orders by count and tops up with defaults", () => {
        expect(mostUsed({65: 1, 66: 5}, [66, 67, 68], 3)).toEqual([66, 65, 67]);
        expect(mostUsed({}, [1, 2, 3], 2)).toEqual([1, 2]);
    });
});

describe("rotateElement", () => {
    test("turns the box about its center and tracks the angle", () => {
        const el = {x: 2, y: 4, w: 20, h: 6};
        rotateElement(el);
        expect(el).toEqual({x: 9, y: -3, w: 6, h: 20, rot: 90});
        rotateElement(el, -1);
        expect(el).toEqual({x: 2, y: 4, w: 20, h: 6, rot: 0});
        rotateElement(el, -1);
        expect(el).toEqual({x: 9, y: -3, w: 6, h: 20, rot: 270});
    });

    test("keeps the box on the dot grid", () => {
        const el = rotateElement({x: 0, y: 0, w: 3, h: 2.125});
        expect(Number.isInteger(el.x * 8) && Number.isInteger(el.y * 8)).toBe(true);
    });
});
