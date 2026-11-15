// The installed app's worker (see sw.js): it only answers a page that cannot
// be reached, and stores nothing. Without it the page works the same.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/web/sw.js", { scope: "/web/" })
    .catch((err) => console.warn("service worker not registered", err));
}

// "Ativar avisos". The server renders it, hidden, only for an unlocked app
// session (a passkey on a phone). It is shown only inside the installed app,
// where iOS lets a page ask for notices, and only while this device has no
// working subscription. The words it says come from the server, on the button.
//
// A subscription the device already holds is handed to the server again on
// every open: revoking access (/web encerrar, removing a passkey) deletes the
// server's row and leaves the device's, so without this, notices would never
// come back after Face ID is turned on again. The server stores it again
// (re-bound to this session's person and passkey) or refuses it, and one
// refused as invalid is dropped here so the button can offer a new one.
const NOTICES = document.querySelector("[data-avisos]");
const NOTICES_NOTE = document.querySelector("[data-avisos-nota]");
const STANDALONE = window.matchMedia("(display-mode: standalone)").matches
  || navigator.standalone === true;

function noticesSay(text) {
  if (!NOTICES_NOTE) return;
  NOTICES_NOTE.textContent = text || "";
  NOTICES_NOTE.hidden = !text;
}

function fromBase64url(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/")
    + "=".repeat((4 - (text.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

// Hands a subscription to the server; the response, or null when the
// network failed.
async function sendSubscription(subscription) {
  const keys = subscription.toJSON().keys || {};
  try {
    return await fetch("/web/avisos/assinar", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf: NOTICES.dataset.csrf, endpoint: subscription.endpoint,
                             p256dh: keys.p256dh, auth: keys.auth }),
    });
  } catch (err) {
    return null;
  }
}

async function offerNotices() {
  if (!NOTICES || !STANDALONE || !("serviceWorker" in navigator)
      || !("PushManager" in window) || !("Notification" in window)) return;
  let registration;
  let key;
  try {
    registration = await navigator.serviceWorker.ready;
    const response = await fetch("/web/avisos/chave", { credentials: "same-origin",
                                                        cache: "no-store" });
    if (!response.ok) return;
    key = fromBase64url((await response.json()).chave);
    const held = await registration.pushManager.getSubscription();
    if (held) {
      // A browser that does not say which key the subscription was made for
      // is taken at its word: dropping it on every open would ask for a tap
      // every time.
      const oldKey = held.options ? held.options.applicationServerKey : undefined;
      const bytes = oldKey ? new Uint8Array(oldKey) : null;
      const matches = oldKey === undefined || (bytes && bytes.length === key.length
        && bytes.every((value, index) => value === key[index]));
      if (matches) {
        const kept = await sendSubscription(held);
        // Network and session refusals do not invalidate a device's keys.
        if (!kept || kept.ok || kept.status !== 400) return;
      }
      // A VAPID key replacement needs a new subscription and a fresh tap.
      if (!await held.unsubscribe()) return;
    }
  } catch (err) {
    return;
  }
  if (Notification.permission === "denied") { noticesSay(NOTICES.dataset.negado); return; }
  NOTICES.hidden = false;
  // subscribe() is the first thing the tap does: iOS asks for permission
  // only from a tap, and an await before it would lose that.
  NOTICES.addEventListener("click", () => subscribeNotices(registration, key));
}

async function subscribeNotices(registration, key) {
  NOTICES.disabled = true;
  noticesSay("");
  let subscription;
  try {
    subscription = await registration.pushManager.subscribe({ userVisibleOnly: true,
                                                             applicationServerKey: key });
  } catch (err) {
    NOTICES.disabled = false;
    if (Notification.permission === "denied") {
      NOTICES.hidden = true;
      noticesSay(NOTICES.dataset.negado);
    } else {
      noticesSay(NOTICES.dataset.falha);
    }
    return;
  }
  const response = await sendSubscription(subscription);
  if (response && response.ok) {
    NOTICES.hidden = true;
    noticesSay(NOTICES.dataset.ativado);
    return;
  }
  // The server did not keep it, so neither does the device: otherwise the
  // button would never show again for a subscription that leads nowhere.
  try { await subscription.unsubscribe(); } catch (err) { /* nothing more to do */ }
  NOTICES.disabled = false;
  noticesSay(NOTICES.dataset.falha);
}

offerNotices();

