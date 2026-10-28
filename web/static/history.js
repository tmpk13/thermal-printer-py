// Undo/redo over snapshots of a plain JSON-like document.

// Copies objects and arrays but shares strings, so image data URLs cost nothing per snapshot.
export function clone(v) {
    if (Array.isArray(v)) return v.map(clone);
    if (v && typeof v === "object") {
        const out = {};
        for (const k in v) out[k] = clone(v[k]);
        return out;
    }
    return v;
}

// Deep equality, ignoring object keys in `skip` at any depth.
export function same(a, b, skip = new Set()) {
    if (a === b) return true;
    if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    if (Array.isArray(a)) return a.length === b.length && a.every((v, i) => same(v, b[i], skip));
    const ka = Object.keys(a).filter((k) => !skip.has(k));
    const kb = Object.keys(b).filter((k) => !skip.has(k));
    return ka.length === kb.length && ka.every((k) => k in b && same(a[k], b[k], skip));
}

// Each step remembers the view (current label, selection) just before and after it, so undo
// returns to where the change was made. Records with the same group less than mergeMs apart
// (typing in one field, holding an arrow key) collapse into one step.
export class EditHistory {
    constructor(state, view, {limit = 200, mergeMs = 1000, skip = new Set()} = {}) {
        this.steps = [{state: clone(state), before: view, after: view}];
        this.i = 0;
        this.view = view;
        this.limit = limit;
        this.mergeMs = mergeMs;
        this.skip = skip;
        this.group = null;
        this.time = 0;
    }

    get canUndo() {
        return this.i > 0;
    }

    get canRedo() {
        return this.i < this.steps.length - 1;
    }

    // Returns true when the state differs from the current step.
    record(state, view, group = null, now = Date.now()) {
        const top = this.steps[this.i];
        const before = this.view;
        this.view = view;
        if (same(state, top.state, this.skip)) return false;
        const merge = group !== null && group === this.group && now - this.time < this.mergeMs && this.i > 0;
        this.group = group;
        this.time = now;
        this.steps.length = this.i + 1;
        if (merge) {
            // Typing a character and deleting it again leaves no step behind.
            if (same(state, this.steps[this.i - 1].state, this.skip)) {
                this.steps.pop();
                this.i--;
                this.group = null;
            } else {
                top.state = clone(state);
                top.after = view;
            }
            return true;
        }
        this.steps.push({state: clone(state), before, after: view});
        if (this.steps.length > this.limit) this.steps.shift();
        this.i = this.steps.length - 1;
        return true;
    }

    // Both return {state, view} with a fresh copy of the state to edit, or null.
    undo() {
        if (!this.canUndo) return null;
        const view = this.steps[this.i].before;
        this.i--;
        return this.#restore(view);
    }

    redo() {
        if (!this.canRedo) return null;
        this.i++;
        return this.#restore(this.steps[this.i].after);
    }

    #restore(view) {
        this.view = view;
        this.group = null;
        return {state: clone(this.steps[this.i].state), view};
    }
}
