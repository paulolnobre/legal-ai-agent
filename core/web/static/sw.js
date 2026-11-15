// The installed app's worker. It does one thing for a person: when a page
// cannot be reached, it says so in the panel's own look instead of the
// browser's error page. It keeps nothing: no copy of any page or answer, no
// data of any kind. The browser keeps this file, which is code, and the
// session cookie, as it does without a worker.
//
// It touches only a page the person navigates to with a GET. Every other
// request, a form POST included, goes to the network as if it were not here.
//
// Its policy (set by the server, not by the page) lets it reach this host and
// nothing else; without that, its own fetch of the page would be refused and
// every page would look offline.

// Drawn like the panel (core/web/pages.py, _DAY and _NIGHT, system serif and
// sans, 12px corners on the note), all in this string: nothing is fetched to show it.
const OFFLINE_HTML = `<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#080808">
<title>Sem conexão</title>
<style>
:root{color-scheme:light;--bg:#f6f5f2;--panel:#fff;--text:#151515;--strong:#000;--mid:#4e4e4e;
 --muted:#6b6b6b;--accent:#1a1a1a}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#080808;--panel:#111;
 --text:#e8e8e8;--strong:#fff;--mid:#a8a8a8;--muted:#8a8a8a;--accent:#bbb}}
*,*::before,*::after{box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
 max-width:30rem;margin:0 auto;padding-block:3.5rem 4.5rem;
 padding-inline:clamp(1.25rem,4vw,2.5rem);line-height:1.6;color:var(--text);
 background:var(--bg);-webkit-font-smoothing:antialiased}
h1{font-family:Georgia,'Times New Roman',serif;font-weight:600;font-size:2rem;
 line-height:1.15;letter-spacing:-.01em;color:var(--strong);margin:0 0 .5rem;
 text-wrap:balance}
p{color:var(--mid)}
p.sub{color:var(--muted);margin:0 0 2rem;font-size:.8rem;letter-spacing:.14em;
 text-transform:uppercase}
.note{margin-top:1.8rem;padding:1rem 1.2rem;background:var(--panel);
 border-left:1px solid var(--accent);border-radius:12px;font-size:.88rem;
 color:var(--mid)}
</style>
</head>
<body>
<p class="sub">Demo &amp; Associados</p>
<h1>Sem conexão.</h1>
<div class="note">Abra de novo quando a internet voltar.</div>
</body>
</html>`;

self.addEventListener("install", () => {
  // A fixed worker should not wait for every window to close.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.mode !== "navigate" || request.method !== "GET") return;
  // The network answers whatever it answers, an error page included; only a
  // network that cannot be reached gets the offline page.
  event.respondWith(fetch(request).catch(() => new Response(OFFLINE_HTML, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    },
  })));
});

// A notice says only which kind of thing is waiting. The title is fixed here
// per kind, and nothing else from the push reaches the screen. WebKit revokes
// a subscription whose pushes show nothing, so every push shows one, also
// when the app is open.
const TITLES = {
  answer: "Resposta pronta",
  djen: "Nova publicação no DJEN",
  deadline: "Prazo próximo",
  access: "Novo pedido de acesso",
};
const HOME = "/web/?app=1";

self.addEventListener("push", (event) => {
  let data = null;
  try {
    data = event.data ? event.data.json() : null;
  } catch {
    data = null;
  }
  if (data === null || typeof data !== "object") data = {};
  const known = typeof data.kind === "string" && Object.hasOwn(TITLES, data.kind);
  const title = known ? TITLES[data.kind] : "Novo aviso";
  const url = typeof data.url === "string" ? data.url : HOME;
  event.waitUntil(self.registration.showNotification(title, {
    data: { url },
    tag: known ? data.kind : "aviso",
  }));
});

// Where a tap may go: a path of this app, under /web/, on this host. A full
// address, one starting with "//" (another host, or this one by a door the
// rule does not name) and a path that climbs out of /web/ all land on the
// assistant instead.
function landing(url) {
  if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) return HOME;
  let parsed;
  try {
    parsed = new URL(url, self.location.origin);
  } catch {
    return HOME;
  }
  if (parsed.origin !== self.location.origin || !parsed.pathname.startsWith("/web/")) {
    return HOME;
  }
  return parsed.pathname + parsed.search + parsed.hash;
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data;
  const target = landing(data && typeof data === "object" ? data.url : undefined);
  event.waitUntil((async () => {
    // A window of the app is navigated, not only focused: the tap makes a
    // request, which meets the session rules (Face ID when locked).
    const open = await self.clients.matchAll({ type: "window" });
    for (const client of open) {
      let moved = null;
      try {
        moved = await client.navigate(target);
      } catch {
        continue;
      }
      if (moved) {
        try {
          await moved.focus();
        } catch {
          // Shown already; focus is the browser's to refuse.
        }
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
