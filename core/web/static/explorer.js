// The explorer, improved. A document opens in a panel beside the cards, a
// card can be dragged onto one of the person's folders, and search answers as
// you type. Each of these is also a plain link or form on the page, so if this
// file never loads, nothing is lost.
//
// What the server sends is put in only as nodes it parsed and escaped; what
// this file writes itself is text (textContent). No eval, no inline handlers:
// the page's policy allows neither.

// The installed app's worker (see sw.js): it only answers a page that cannot
// be reached, and stores nothing. Without it the page works the same.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/web/sw.js", { scope: "/web/" })
    .catch((err) => console.warn("service worker not registered", err));
}

const HOME = "/web/explorar/";
const APP_LOCK = !!document.querySelector('meta[name="app-lock"][content="1"]');
let leaving = false;
let checkingSession = false;

function faceId() {
  if (leaving) return;
  leaving = true;
  document.body.replaceChildren();
  location.replace("/web/faceid/?volta=" + encodeURIComponent(location.pathname + location.hash));
}

async function checkSession() {
  if (leaving || checkingSession || document.visibilityState !== "visible") return;
  checkingSession = true;
  try {
    const response = await fetch("/web/sessao", { credentials: "same-origin", cache: "no-store" });
    if (response.status !== 204) faceId();
  } catch (err) {
    faceId();
  } finally {
    checkingSession = false;
  }
}

if (APP_LOCK) {
  document.addEventListener("visibilitychange", checkSession);
  window.addEventListener("pageshow", checkSession);
}

function csrf() {
  // The form token every POST carries; the bar's "Sair" form holds it.
  const field = document.querySelector("nav form input[name=csrf]");
  return field ? field.value : "";
}

async function fetchPage(url, signal) {
  const response = await fetch(url, { credentials: "same-origin", signal });
  if (leaving) return null;
  if (response.status === 401) {
    // The session ended: let the browser show the page that says so.
    location.assign(url);
    return null;
  }
  const doc = new DOMParser().parseFromString(await response.text(), "text/html");
  return { status: response.status, doc };
}

// ── A message at the top ────────────────────────────────────────────────────

const toast = document.createElement("div");
toast.id = "toast";
toast.setAttribute("role", "status");
toast.hidden = true;
document.body.append(toast);
let toastTimer;

function say(text, bad = false) {
  toast.textContent = text;
  toast.classList.toggle("bad", bad);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
}

// A move reloads the page so cards, counts and the breadcrumb are all fresh;
// what to say about it survives the reload here.
try {
  const left = sessionStorage.getItem("explorer:said");
  if (left) { sessionStorage.removeItem("explorer:said"); say(left); }
} catch (err) { /* storage off: no message, nothing else lost */ }

// ── The panel ───────────────────────────────────────────────────────────────

const drawer = document.createElement("aside");
drawer.className = "drawer";
drawer.setAttribute("role", "dialog");
drawer.hidden = true;
const closeButton = document.createElement("button");
closeButton.type = "button";
closeButton.className = "quiet close";
closeButton.textContent = "Fechar";
const drawerBody = document.createElement("div");
drawer.append(closeButton, drawerBody);
document.body.append(drawer);
let opener = null;
let opening = null;
let openings = 0;

// Beside the cards on a wide screen, the panel leaves them usable, so the
// next document is one click away. Where it covers the whole page, what is
// behind it is made inert, so Tab cannot reach controls nobody can see.
function setModal(on) {
  drawer.setAttribute("aria-modal", on ? "true" : "false");
  for (const child of document.body.children) {
    if (child !== drawer && child !== toast) child.inert = on;
  }
}

function closeDrawer() {
  openings += 1;  // a request still on its way must not reopen it
  if (opening) { opening.abort(); opening = null; }
  drawer.hidden = true;
  setModal(false);
  drawerBody.replaceChildren();
  if (opener && document.contains(opener)) opener.focus();
  opener = null;
}

async function openDrawer(link) {
  if (opening) opening.abort();
  opening = new AbortController();
  const mine = ++openings;
  let page;
  try {
    page = await fetchPage(link.href, opening.signal);
  } catch (err) {
    if (err.name !== "AbortError" && mine === openings) location.assign(link.href);
    return;
  }
  if (!page || mine !== openings) return;
  const article = page.doc.getElementById("document");
  if (page.status !== 200 || !article) {
    // Not found, refused, or anything unexpected: the page itself says why.
    location.assign(link.href);
    return;
  }
  opener = link;
  drawerBody.replaceChildren(document.importNode(article, true));
  drawer.setAttribute("aria-label", page.doc.title);
  drawer.hidden = false;
  drawer.scrollTop = 0;
  setModal(drawer.getBoundingClientRect().left <= 0);
  const heading = drawerBody.querySelector("h1");
  if (heading) { heading.tabIndex = -1; heading.focus(); }
}

closeButton.addEventListener("click", closeDrawer);
// Turning a phone or resizing a window can make the open panel start or stop
// covering the page; which parts stay usable follows it.
window.addEventListener("resize", () => {
  if (!drawer.hidden) setModal(drawer.getBoundingClientRect().left <= 0);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !drawer.hidden) closeDrawer();
});
document.addEventListener("click", (event) => {
  const link = event.target.closest(".card a.title");
  if (!link || event.defaultPrevented || event.button !== 0
      || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openDrawer(link);
});

// ── Dragging a card onto a folder ───────────────────────────────────────────

const bar = document.getElementById("drop");

function makeDraggable(root) {
  if (!bar) return;
  for (const card of root.querySelectorAll(".card[data-id]")) card.draggable = true;
}

async function move(id, from, target) {
  const slug = target.dataset.slug;
  if (!slug || slug === from) return;
  // visivel=1: the server refuses a drop that would take the document out of
  // the mover's sight (a team they left since the page loaded), since a drag
  // gives no warning. "Mover para..." is where that move is made, warned.
  const body = new URLSearchParams({ csrf: csrf(), destino: slug, visivel: "1" });
  let response;
  try {
    response = await fetch(`${HOME}documento/${encodeURIComponent(id)}/mover`, {
      method: "POST", body, credentials: "same-origin", redirect: "manual",
    });
  } catch (err) {
    say("Não consegui falar com o servidor. Tente de novo.", true);
    return;
  }
  // The move answers with a redirect; anything else is a refusal that
  // carries its reason in the page it sends.
  if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
    try { sessionStorage.setItem("explorer:said", `Documento movido para: ${target.textContent}.`); }
    catch (err) { /* the reload still shows where it went */ }
    location.reload();
    return;
  }
  if (response.status === 401) { location.reload(); return; }
  const doc = new DOMParser().parseFromString(await response.text(), "text/html");
  const reason = doc.querySelector("h1.bad + p");
  say(reason ? reason.textContent : "Não consegui mover o documento.", true);
}

if (bar) {
  makeDraggable(document);
  let dragged = null;
  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest && event.target.closest(".card[data-id]");
    if (!card) return;
    dragged = card;
    card.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", card.dataset.id);
    for (const target of bar.querySelectorAll(".target")) {
      target.hidden = target.dataset.slug === card.dataset.folder;
    }
    bar.hidden = false;
  });
  document.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    bar.hidden = true;
    for (const target of bar.querySelectorAll(".target")) target.classList.remove("over");
  });
  for (const target of bar.querySelectorAll(".target")) {
    target.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      target.classList.add("over");
    });
    target.addEventListener("dragleave", () => target.classList.remove("over"));
    target.addEventListener("drop", (event) => {
      event.preventDefault();
      const card = dragged;
      if (card) move(card.dataset.id, card.dataset.folder, target);
    });
  }
}

// ── Search as you type ──────────────────────────────────────────────────────

const input = document.getElementById("q");
if (input && document.getElementById("results")) {
  let timer;
  let searching = null;
  let searches = 0;
  const run = async () => {
    searching = new AbortController();
    const mine = ++searches;
    const text = input.value.trim();
    const url = text ? `${HOME}?q=${encodeURIComponent(text)}` : HOME;
    let page;
    try {
      page = await fetchPage(url, searching.signal);
    } catch (err) {
      return;  // aborted by a newer keystroke, or offline: the form still works
    }
    // Only the answer to what is in the box now: an older one that arrives
    // late is dropped, results and address alike.
    if (!page || page.status !== 200 || mine !== searches) return;
    const fresh = page.doc.getElementById("results");
    const current = document.getElementById("results");
    if (!fresh || !current) return;
    const placed = document.importNode(fresh, true);
    current.replaceWith(placed);
    makeDraggable(placed);
    history.replaceState(null, "", url);
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    searches += 1;  // whatever is on its way now answers an older text
    if (searching) { searching.abort(); searching = null; }
    timer = setTimeout(run, 300);
  });
}

// ── Editing: a law has a sphere, anything else a client ────────────────────

// core/legislation.py's fold, the same ranges spelled the same way: decompose,
// drop these combining marks, lowercase, these spaces to one plain space,
// plain spaces off the ends. The tests run this function against Python's.
function foldType(text) {
  return text.normalize("NFD")
    .replace(/[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]/g, "")
    .toLowerCase()
    .replace(/[\u0020\u0009\u000a\u000d\u000c\u000b\u00a0]+/g, " ")
    .replace(/^ +| +$/g, "");
}

const editForm = document.querySelector("form.edit[data-legislacao]");
if (editForm) {
  let starts = [];
  try { starts = JSON.parse(editForm.dataset.legislacao); } catch (err) { starts = []; }
  const isLegislation = (text) => {
    const folded = foldType(text);
    return starts.some((start) => folded === start || folded.startsWith(`${start} `)
      || folded.startsWith(`${start}-`));
  };
  const typeField = editForm.querySelector("#tipo");
  const sync = () => {
    const law = typeField && isLegislation(typeField.value);
    for (const field of editForm.querySelectorAll("[data-campo=cliente]")) field.hidden = law;
    for (const field of editForm.querySelectorAll("[data-campo=esfera]")) field.hidden = !law;
  };
  if (typeField) typeField.addEventListener("input", sync);
  sync();
}
