// Face ID: enrolling a passkey on the home page, signing in and unlocking on
// the Face ID page. Each ceremony is two requests: the options (with a
// challenge the server keeps), then what the device signed. The server
// answers JSON, never a redirect, and this file moves the page itself.
//
// What this file writes into the page is text (textContent), never markup.
// A button stays hidden unless this browser can do its ceremony. On a phone,
// sign-in shows only in the installed app, where a Telegram link cannot be
// opened; on a computer it shows in a tab and in the app alike. That is
// presentation only: the server decides nothing from it. The one thing the
// server reads from here is the touch report sent with a sign-in, and it
// counts a missing one as a phone.

const REFUSED =
  "O Face ID não confirmou. Tente de novo, ou entre pelo link do /web no Telegram.";

const CEREMONIES = {
  ativar: { path: "/web/faceid/ativar", create: true },
  entrar: { path: "/web/faceid/entrar", create: false, signIn: true },
  "entrar-computador": { path: "/web/faceid/entrar", create: false, signIn: true },
  desbloquear: { path: "/web/faceid/desbloquear", create: false },
};

function toBytes(text) {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

function toText(buffer) {
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function described(list) {
  return (list || []).map((item) => ({ ...item, id: toBytes(item.id) }));
}

function creationOptions(options) {
  return {
    ...options,
    challenge: toBytes(options.challenge),
    user: { ...options.user, id: toBytes(options.user.id) },
    excludeCredentials: described(options.excludeCredentials),
  };
}

function requestOptions(options) {
  return {
    ...options,
    challenge: toBytes(options.challenge),
    allowCredentials: described(options.allowCredentials),
  };
}

function asJSON(credential) {
  const r = credential.response;
  const response = { clientDataJSON: toText(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = toText(r.attestationObject);
    response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
  } else {
    response.authenticatorData = toText(r.authenticatorData);
    response.signature = toText(r.signature);
    response.userHandle = r.userHandle ? toText(r.userHandle) : null;
  }
  return {
    id: credential.id,
    rawId: toText(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || undefined,
    clientExtensionResults: credential.getClientExtensionResults(),
    response,
  };
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  let answer = {};
  try {
    answer = await response.json();
  } catch {
    answer = {};
  }
  return { ok: response.ok, answer: answer && typeof answer === "object" ? answer : {} };
}

async function run(button, ceremony, say, refused) {
  const base = { csrf: button.dataset.csrf || "" };
  const asked = await post(`${ceremony.path}/opcoes`, base);
  if (!asked.ok) return say(asked.answer.erro || refused, asked.answer.aparelhos);
  // A cancel, a timeout or a refused Face ID all throw here, and read the
  // same: the challenge simply expires unanswered.
  const credential = ceremony.create
    ? await navigator.credentials.create({ publicKey: creationOptions(asked.answer) })
    : await navigator.credentials.get({ publicKey: requestOptions(asked.answer) });
  const done = await post(ceremony.path, {
    ...base,
    volta: button.dataset.volta || "",
    credencial: asJSON(credential),
    // No Mac has a touch screen: an iPhone asking for the desktop site
    // sends a Mac's User-Agent and is told apart by this.
    ...(ceremony.signIn ? { toque: navigator.maxTouchPoints } : {}),
  });
  const where = done.answer.ir;
  // Only ever a page of the panel, whatever the answer says.
  if (done.ok && typeof where === "string" && where.startsWith("/web/")) {
    location.replace(where);
    return;
  }
  say(done.answer.erro || refused, done.answer.aparelhos);
}

const supported = "PublicKeyCredential" in window && !!navigator.credentials;
const appWindow = matchMedia("(display-mode: standalone)").matches;
const standalone = appWindow || navigator.standalone === true;
// Safari's Dock app on a Mac: navigator.standalone exists only on iPhone and
// iPad, and every other browser on a Mac names itself besides "Safari".
const agent = navigator.userAgent;
const safariDockApp =
  appWindow &&
  navigator.standalone === undefined &&
  /Safari\//.test(agent) &&
  !/Chrome|Chromium|CriOS|Edg|OPR|Firefox|FxiOS|Android/.test(agent);

for (const button of document.querySelectorAll("button[data-faceid]")) {
  const ceremony = CEREMONIES[button.dataset.faceid];
  const section = button.closest("section");
  if (!ceremony || !section || !supported) continue;
  if (button.dataset.faceid === "entrar" && !standalone) continue;
  const note = section.querySelector("[role=alert]");
  const say = (text, devices) => {
    if (!note) return;
    note.textContent = text;
    if (devices === "/web/aparelhos/") {
      const link = document.createElement("a");
      link.href = devices;
      link.textContent = "Aparelhos";
      note.append(" ", link);
    }
    note.hidden = !text;
  };
  // The page's words for a refusal on this device; Face ID's if it gave none.
  const refused = button.dataset.recusa || REFUSED;
  const dock = section.querySelector("[data-dock]");
  if (dock) dock.hidden = !safariDockApp;
  section.hidden = false;
  button.addEventListener("click", async () => {
    button.disabled = true;
    say("");
    try {
      await run(button, ceremony, say, refused);
    } catch {
      say(refused);
    } finally {
      button.disabled = false;
    }
  });
}
