// Tiny shared store for the loaded trending companies, so both the home list
// (app.js) and the per-company page (router.js) read the same data without
// re-fetching and without importing each other.

let _items = [];
let _resolveReady;
export const ready = new Promise((res) => { _resolveReady = res; });

export function setItems(items) {
    _items = Array.isArray(items) ? items : [];
    _resolveReady(_items);
}

export function getItems() { return _items; }

// Resolve a company by ticker (case-insensitive; matches primary ticker or any
// of the merged dual-class tickers) or by CIK.
export function findByTicker(t) {
    if (!t) return null;
    const q = String(t).toUpperCase();
    return _items.find((it) => {
        const tks = (it.tickers && it.tickers.length) ? it.tickers : [it.ticker];
        return tks.some((x) => String(x).toUpperCase() === q);
    }) || null;
}

export function findByCik(cik) {
    if (!cik) return null;
    const norm = (x) => String(x).replace(/^0+/, "");   // tolerate unpadded CIK in the URL
    const q = norm(cik);
    return _items.find((it) => norm(it.cik) === q) || null;
}
