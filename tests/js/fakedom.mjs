// A tiny stand-in for the browser DOM, just enough to render and click the dashboard's own code in Node.
// It behaves like the real thing where the code relies on it: adding a node moves it, `hidden` and
// `disabled` are properties that mirror attributes, events can be fired with fire().

export class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.style = {}; this.listeners = {}; this.className = ""; this.text = ""; }
  get id() { return this.attrs.id ?? ""; }
  set id(v) { this.attrs.id = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  closest(sel) { return null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(type, fn, opts) {
    const list = (this.listeners[type] ||= []);
    if (opts && opts.once) {
      const wrapped = (e) => { this.listeners[type] = this.listeners[type].filter((f) => f !== wrapped); fn(e); };
      list.push(wrapped);
    } else list.push(fn);
  }
  fire(type, event = {}) {
    const e = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...event };
    (this.listeners[type] || []).slice().forEach((fn) => fn(e));
    return e;
  }
  click() { (this.listeners.click || []).forEach((fn) => fn({ target: this })); }
  detach(node) { if (node.parent) node.parent.children = node.parent.children.filter((c) => c !== node); node.parent = this; }
  append(...kids) { kids.forEach((k) => this.detach(k)); this.children.push(...kids); }      // like the real DOM, adding a node moves it
  replaceChildren(...kids) { kids.forEach((k) => this.detach(k)); this.children = kids; }
  insertBefore(node, ref) { this.detach(node); const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, node); }
  get parentNode() { return this.parent ?? null; }
  get firstChild() { return this.children[0] ?? null; }
  get disabled() { return "disabled" in this.attrs; }
  set disabled(v) { if (v) this.attrs.disabled = ""; else delete this.attrs.disabled; }
  get value() { return this.attrs.value ?? this._value ?? ""; }
  set value(v) { this._value = String(v); }
  get hidden() { return "hidden" in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ""; else delete this.attrs.hidden; }
  get textContent() { return this.tag === "#text" ? this.text : this.text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.text = String(v); this.children = []; }
  get classList() {
    const node = this, names = () => node.className.split(/\s+/).filter(Boolean);
    return {
      add: (n) => { if (!names().includes(n)) node.className = [...names(), n].join(" "); },
      contains: (n) => names().includes(n),
      toggle: (n, on) => { const has = names().includes(n); if (on && !has) node.className = [...names(), n].join(" "); if (!on && has) node.className = names().filter((x) => x !== n).join(" "); },
    };
  }
  walk(visit) { visit(this); this.children.forEach((c) => c.walk && c.walk(visit)); }
  find(pred) { const out = []; this.walk((n) => { if (pred(n)) out.push(n); }); return out; }
  querySelector(tag) { return this.find((n) => n.tag === tag)[0] ?? null; }
  hasClass(name) { return this.classList.contains(name); }
}

export function installFakeDom() {
  const saved = new Map();
  globalThis.Node = FakeNode;
  globalThis.document = { createElement: (tag) => new FakeNode(tag), createTextNode: (t) => { const n = new FakeNode("#text"); n.text = t; return n; } };
  globalThis.window = { devicePixelRatio: 1 };
  globalThis.ResizeObserver = class { observe() {} };
  globalThis.localStorage = { getItem: (k) => saved.get(k) ?? null, setItem: (k, v) => saved.set(k, String(v)) };
  return { saved };
}
