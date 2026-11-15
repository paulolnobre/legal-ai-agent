// Runs core/web/static/navigation.js against the smallest stand-in for the
// page it needs, clicks "Tema" a few times and prints what changed.
// Usage: node navigation_harness.mjs <navigation.js> '<json scenario>'
// scenario: {mobile, theme, menuOpen, clicks, fetch: "ok" | "refused" | "offline"}
import { pathToFileURL } from "node:url";

const [scriptPath, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const errors = [];
process.on("uncaughtException", (error) => errors.push(String(error)));
process.on("unhandledRejection", (error) => errors.push(String(error)));

class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.attrs = { ...attrs };
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.hidden = false;
    this.textContent = "";
    this.dataset = {};
    this.classList = new Set();
    this.classList.add = Set.prototype.add;
    this.classList.remove = Set.prototype.delete;
  }
  append(child) {
    if (child.parent) child.parent.children = child.parent.children.filter((c) => c !== child);
    child.parent = this;
    this.children.push(child);
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  get href() { return this.attrs.href; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  matches(selector) {
    if (selector === "a.theme") return this.tagName === "A" && this.attrs.class === "theme";
    if (selector === "a") return this.tagName === "A";
    if (selector === "span") return this.tagName === "SPAN";
    if (selector === "meta[name=theme-color]") return this.tagName === "META" && this.name === "theme-color";
    if (selector.startsWith(".")) return this.attrs.class === selector.slice(1);
    if (selector.startsWith("[")) return selector.slice(1, -1) in this.attrs;
    return false;
  }
  closest(selector) {
    for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
    return null;
  }
  *walk() { for (const child of this.children) { yield child; yield* child.walk(); } }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { return [...this.walk()].filter((node) => node.matches(selector)); }
  getClientRects() { return [1]; }
  focus() {}
  click() {
    const event = {
      type: "click", target: this, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.stopped = true; },
    };
    for (let node = this; node && !event.stopped; node = node.parent) {
      for (const listener of node.listeners.click || []) listener(event);
    }
    if (!event.defaultPrevented && this.tagName === "A") followed.push(this.attrs.href);
  }
}

class Dialog extends Element {
  constructor(attrs) { super("dialog", attrs); this.open = false; }
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    for (const listener of this.listeners.close || []) listener({});
  }
  getBoundingClientRect() { return { left: 0, right: 300, top: 0, bottom: 800 }; }
}

const followed = [];
const assigned = [];
const fetched = [];
const root = new Element("html");
const head = new Element("head");
const body = new Element("body");
root.append(head);
root.append(body);
if (scenario.theme) root.dataset.theme = scenario.theme;
for (const media of ["light", "dark"]) {
  const meta = new Element("meta");
  meta.name = "theme-color";
  meta.media = `(prefers-color-scheme: ${media})`;
  meta.content = "old";
  head.append(meta);
}

const openButton = new Element("button", { "data-menu-open": "" });
const slot = new Element("div", { class: "sidebar-slot" });
const navigation = new Element("nav", { class: "sidebar-nav" });
const closeButton = new Element("button", { "data-menu-close": "" });
const themeLink = new Element("a", { class: "theme", href: "/web/tema?volta=%2Fweb%2Fassistente%2F" });
const themeLabel = new Element("span");
themeLabel.textContent = "Tema: inicial";
const home = new Element("a", { href: "/web/" });
const dialog = new Dialog({ id: "app-menu" });
body.append(openButton);
body.append(slot);
body.append(dialog);
slot.append(navigation);
navigation.append(closeButton);
navigation.append(home);
navigation.append(themeLink);
themeLink.append(themeLabel);

globalThis.document = {
  documentElement: root,
  head,
  body,
  querySelector: (selector) => root.querySelector(selector),
  querySelectorAll: (selector) => root.querySelectorAll(selector),
  getElementById: (id) => [...root.walk()].find((node) => node.attrs.id === id) || null,
  createElement: (tag) => new Element(tag),
  get activeElement() { return null; },
};
globalThis.window = { addEventListener() {} };
globalThis.matchMedia = () => ({ matches: Boolean(scenario.mobile), addEventListener() {} });
globalThis.location = { assign: (url) => assigned.push(url) };
globalThis.fetch = async (url, options) => {
  fetched.push({ url, options });
  if (scenario.fetch === "offline") throw new TypeError("Failed to fetch");
  return { status: scenario.fetch === "refused" ? 400 : 204 };
};

await import(pathToFileURL(scriptPath).href);
if (scenario.menuOpen) openButton.click();

const steps = [];
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
for (let i = 0; i < (scenario.clicks ?? 1); i += 1) {
  themeLink.click();
  await settle();
  await settle();
  steps.push({
    theme: root.dataset.theme ?? "",
    label: themeLabel.textContent,
    bar: head.querySelectorAll("meta[name=theme-color]").map((m) => [m.media || "", m.content]),
    menuOpen: dialog.open,
  });
}

console.log(JSON.stringify({
  steps, followed, assigned, errors,
  fetched: fetched.map((f) => ({ url: f.url, credentials: f.options?.credentials })),
}));
