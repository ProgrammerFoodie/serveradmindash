// Drag-and-drop ordering for the cards of a grid. Pointer events, so mouse, pen and touch behave the same;
// the grip also works from the keyboard (arrow keys). While dragging, the card stays where it is and the
// card under the pointer shows where the drop will land; moving it only on release avoids the flicker
// of live reordering in a grid whose cards have different sizes.
//
// The ordering rules are plain functions of key lists, so they can be tested without a browser.

/** `current` is the keys in their present order; `saved` an earlier order that may be stale.
 *  Saved keys that still exist keep their saved order; new keys go right after their predecessor in `current`. */
export function applyOrder(current, saved) {
  const result = [];
  for (const key of saved || []) if (current.includes(key) && !result.includes(key)) result.push(key);
  current.forEach((key, i) => {
    if (result.includes(key)) return;
    let at = 0;
    for (let j = i - 1; j >= 0; j--) { const found = result.indexOf(current[j]); if (found >= 0) { at = found + 1; break; } }
    result.splice(at, 0, key);
  });
  return result;
}

/** `order` with `key` moved to just before or after `target`. */
export function moveKey(order, key, target, after) {
  if (key === target || !order.includes(key) || !order.includes(target)) return order.slice();
  const rest = order.filter((k) => k !== key);
  rest.splice(rest.indexOf(target) + (after ? 1 : 0), 0, key);
  return rest;
}

/** The nearest visible key before (dir -1) or after (dir 1) `key`, or null. */
export function neighbourKey(order, visible, key, dir) {
  for (let i = order.indexOf(key) + dir; i >= 0 && i < order.length; i += dir) if (visible.has(order[i])) return order[i];
  return null;
}

const GRIP = ".grip";
const edge = 70;      // px from the top or bottom of the window where dragging scrolls the page

/**
 * Make the cards of `container` (children with a data-key) sortable by their .grip button.
 * load() returns the saved key list, save(keys) stores it, defaults() the keys in their natural order.
 * Returns { reapply, reset }.
 */
export function makeSortable(container, { load, save, defaults }) {
  const keyOf = (node) => node.getAttribute("data-key");
  const cards = () => [...container.children].filter((n) => n.getAttribute && n.getAttribute("data-key"));
  const order = () => cards().map(keyOf);
  const place = (keys) => {
    const byKey = new Map(cards().map((n) => [keyOf(n), n]));
    for (const key of keys) container.append(byKey.get(key));
  };
  const commit = (keys) => { place(keys); save(keys); };
  const reapply = () => place(applyOrder(order(), load()));

  let drag = null;
  const mark = (node, where) => { if (where) node.setAttribute("data-drop", where); else node.removeAttribute("data-drop"); };
  const clearMarks = () => cards().forEach((n) => mark(n, null));

  function stop() {
    if (!drag) return;
    drag.card.classList.toggle("dragging", false);
    document.body.classList.toggle("dragging-cards", false);
    clearMarks();
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", stop);
    window.removeEventListener("keydown", onKey);
    drag = null;
  }

  function onMove(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    if (e.clientY < edge) window.scrollBy(0, -16); else if (e.clientY > window.innerHeight - edge) window.scrollBy(0, 16);
    const under = document.elementFromPoint(e.clientX, e.clientY);
    const target = under && under.closest ? under.closest("[data-key]") : null;
    clearMarks();
    drag.target = null;
    if (!target || target === drag.card || target.parentNode !== container || target.hidden) return;
    const box = target.getBoundingClientRect();
    const stacked = box.width > container.clientWidth * 0.6;          // a single column: decide by height instead of width
    drag.after = stacked ? e.clientY > box.top + box.height / 2 : e.clientX > box.left + box.width / 2;
    drag.target = target;
    mark(target, stacked ? (drag.after ? "bottom" : "top") : (drag.after ? "right" : "left"));
  }

  function onUp() {
    const { card, target, after } = drag;
    stop();
    if (target) commit(moveKey(order(), keyOf(card), keyOf(target), after));
  }

  function onKey(e) { if (e.key === "Escape") stop(); }

  container.addEventListener("pointerdown", (e) => {
    const grip = e.target.closest ? e.target.closest(GRIP) : null;
    const card = grip && grip.closest("[data-key]");
    if (!card || (e.button || 0) !== 0) return;
    e.preventDefault();
    drag = { card, pointer: e.pointerId, target: null, after: false };
    card.classList.toggle("dragging", true);
    document.body.classList.toggle("dragging-cards", true);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", stop);
    window.addEventListener("keydown", onKey);
  });

  container.addEventListener("keydown", (e) => {
    const grip = e.target.closest ? e.target.closest(GRIP) : null;
    const dir = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
    if (!grip || !dir) return;
    e.preventDefault();
    const key = keyOf(grip.closest("[data-key]"));
    const target = neighbourKey(order(), new Set(cards().filter((n) => !n.hidden).map(keyOf)), key, dir);
    if (target) { commit(moveKey(order(), key, target, dir > 0)); grip.focus(); }
  });

  return { reapply, reset() { save([]); place(applyOrder(order(), defaults())); } };
}
