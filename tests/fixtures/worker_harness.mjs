// Runs core/web/static/sw.js under Node with just enough of a service worker
// around it for its notice handlers: the registration's showNotification,
// the window clients and openWindow, and the worker's own origin. Fires the
// scenario's events in order and prints what the worker did, as JSON.
//
// node worker_harness.mjs <sw.js> '<scenario JSON>'
//
// scenario: {origin, events: [
//   {type: "push", json: <any> | raw: "<text>" | none: true},
//   {type: "click", data: <notification data>, windows: [
//      {url, navigate: "ok" | "null" | "throw", focus: "ok" | "throw"}]}]}
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [scriptPath, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const origin = scenario.origin || "https://painel.example";
const state = { shown: [], navigated: [], focused: [], opened: [], closed: 0, errors: [] };
const listeners = {};

const clients = {
  matchAll: async (options) => {
    state.match_options = options;
    return current.windows || [];
  },
  openWindow: async (url) => { state.opened.push(url); return null; },
  claim: async () => {},
};
let current = {};

function windowClient(given) {
  const client = {
    url: given.url,
    focus: async () => {
      if (given.focus === "throw") throw new Error("InvalidAccessError");
      state.focused.push(client.url);
      return client;
    },
    navigate: async (url) => {
      if (given.navigate === "throw") throw new TypeError("not controlled");
      state.navigated.push(url);
      if (given.navigate === "null") return null;
      client.url = new URL(url, origin).href;
      return client;
    },
  };
  return client;
}

const self = {
  location: new URL("/web/sw.js", origin),
  registration: {
    showNotification: async (title, options) => {
      state.shown.push({ title, options: JSON.parse(JSON.stringify(options)) });
    },
  },
  clients,
  skipWaiting: () => {},
  addEventListener: (type, fn) => { listeners[type] = fn; },
};
const context = vm.createContext({ self, URL, Response: class {}, fetch: async () => null });
vm.runInContext(readFileSync(scriptPath, "utf8"), context, { filename: "sw.js" });

for (const event of scenario.events) {
  const pending = [];
  const waitUntil = (promise) => pending.push(promise);
  try {
    if (event.type === "push") {
      let data = null;
      if (!event.none) {
        const text = "raw" in event ? event.raw : JSON.stringify(event.json);
        data = { json: () => JSON.parse(text), text: () => text };
      }
      listeners.push({ data, waitUntil });
    } else if (event.type === "click") {
      current = { windows: (event.windows || []).map(windowClient) };
      const notification = { data: event.data, close: () => { state.closed += 1; } };
      listeners.notificationclick({ notification, waitUntil });
    }
    await Promise.all(pending);
  } catch (error) {
    state.errors.push(String(error));
  }
}
process.stdout.write(JSON.stringify(state));
