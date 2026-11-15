// Runs core/web/static/notices.js under Node with just enough of a browser
// for its notices code: the "Ativar avisos" button and its note, display
// mode, the service worker's push manager, Notification and fetch. Prints
// what the page ended up showing and every request it made, as JSON.
//
// node notices_harness.mjs <notices.js> '<scenario JSON>'
//
// scenario: {standalone, csrf, held: null | {endpoint, p256dh, auth},
//            post_status (answer to /web/avisos/assinar), permission,
//            click (press the button once it shows), new: {endpoint, ...}}
import { pathToFileURL } from "node:url";

const [scriptPath, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const state = { requests: [], unsubscribed: [], subscribed: 0 };

function element(dataset) {
  const listeners = {};
  return {
    hidden: true, disabled: false, textContent: "", dataset,
    addEventListener(type, fn) { listeners[type] = fn; },
    listeners,
  };
}

const button = element({ csrf: scenario.csrf || "the-csrf", negado: "NEGADO", ativado: "ATIVADO",
                         falha: "FALHA" });
const note = element({});

function subscription(given) {
  return {
    endpoint: given.endpoint,
    options: given.no_options ? undefined : { applicationServerKey: Uint8Array.from(
      Buffer.from(given.applicationKey || "BAAA", "base64url")).buffer },
    toJSON: () => ({ endpoint: given.endpoint, keys: { p256dh: given.p256dh, auth: given.auth } }),
    unsubscribe: async () => { state.unsubscribed.push(given.endpoint); return true; },
  };
}

const registration = {
  pushManager: {
    getSubscription: async () => (scenario.held ? subscription(scenario.held) : null),
    subscribe: async (options) => {
      state.subscribed += 1;
      state.subscribe_options = { userVisibleOnly: options.userVisibleOnly,
        applicationServerKey: [...options.applicationServerKey] };
      if (scenario.permission === "denied") throw new Error("NotAllowedError");
      return subscription(scenario.new);
    },
  },
};

const page = {
  querySelector(selector) {
    if (selector === "[data-avisos]") return button;
    if (selector === "[data-avisos-nota]") return note;
    return null;
  },
  getElementById() { return null; },
  addEventListener() {},
};

Object.defineProperty(globalThis, "navigator", {
  configurable: true, writable: true,
  value: { standalone: false,
           serviceWorker: { register: async () => registration, ready: Promise.resolve(registration) } },
});
globalThis.window = globalThis;
globalThis.document = page;
globalThis.matchMedia = (query) => ({
  matches: query === "(display-mode: standalone)" && !!scenario.standalone,
});
globalThis.PushManager = function PushManager() {};
globalThis.Notification = { permission: scenario.permission || "default" };
globalThis.addEventListener = () => {};
globalThis.fetch = async (url, options = {}) => {
  state.requests.push({ url, method: options.method || "GET",
                        body: options.body ? JSON.parse(options.body) : null });
  if (url === "/web/avisos/chave") {
    const status = scenario.key_status ?? 200;
    return { ok: status === 200, status,
      json: async () => ({ chave: scenario.key || "BAAA" }) };
  }
  if (url === "/web/avisos/assinar") {
    const status = scenario.post_status ?? 204;
    return { ok: status >= 200 && status < 300, status };
  }
  return { ok: false, status: 404 };
};

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

await import(pathToFileURL(scriptPath).href);
await settle();
if (scenario.click && !button.hidden && button.listeners.click) {
  button.listeners.click();
  await settle();
}
process.stdout.write(JSON.stringify({
  button_hidden: button.hidden, note: note.hidden ? null : note.textContent, ...state,
}));
