// One navigation, fixed beside desktop pages and moved into a native modal
// on phones. No stored preferences, document reads or HTML writes; its one
// request saves the theme. Without this file, the phone gets an ordinary
// navigation above its page and "Tema" is a link that reloads it.
const slot = document.querySelector(".sidebar-slot");
const navigation = slot && slot.querySelector(".sidebar-nav");
const dialog = document.getElementById("app-menu");
const openButton = document.querySelector("[data-menu-open]");
const closeButton = navigation && navigation.querySelector("[data-menu-close]");

if (slot && navigation && dialog && openButton && closeButton) {
  const mobile = matchMedia("(max-width: 800px)");

  function close() {
    if (dialog.open) dialog.close();
  }

  function sync() {
    close();
    if (mobile.matches) dialog.append(navigation);
    else slot.append(navigation);
    openButton.hidden = !mobile.matches;
    closeButton.hidden = !mobile.matches;
    document.body.classList.add("nav-ready");
  }

  openButton.addEventListener("click", () => {
    if (!mobile.matches || dialog.open) return;
    dialog.showModal();
    openButton.setAttribute("aria-expanded", "true");
    document.body.classList.add("menu-open");
  });

  closeButton.addEventListener("click", close);
  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) close();
  });
  dialog.addEventListener("click", (event) => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right
        || event.clientY < bounds.top || event.clientY > bounds.bottom)) close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = [...navigation.querySelectorAll("a[href], button:not([disabled])")]
      .filter((element) => element.getClientRects().length);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  dialog.addEventListener("close", () => {
    openButton.setAttribute("aria-expanded", "false");
    document.body.classList.remove("menu-open");
    if (mobile.matches && openButton.isConnected) openButton.focus({ preventScroll: true });
  });
  // "Tema" repaints in place: a reload closed the menu and sent the
  // assistant's conversation back to its first message.
  const themeLink = navigation.querySelector("a.theme");
  const themeLabel = themeLink && themeLink.querySelector("span");
  if (themeLink && themeLabel) {
    const root = document.documentElement;
    const next = { "": "light", light: "dark", dark: "" };
    const saved = { "": "auto", light: "claro", dark: "escuro" };
    const label = { "": "automático", light: "claro", dark: "escuro" };
    const ground = { light: "#f6f5f2", dark: "#080808" };
    let saving = false;

    function paintBar(theme) {
      for (const meta of document.querySelectorAll("meta[name=theme-color]")) meta.remove();
      for (const name of theme ? [theme] : ["light", "dark"]) {
        const meta = document.createElement("meta");
        meta.name = "theme-color";
        meta.content = ground[name];
        if (!theme) meta.media = `(prefers-color-scheme: ${name})`;
        document.head.append(meta);
      }
    }

    themeLink.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (saving) return;
      saving = true;
      const theme = next[root.dataset.theme || ""] ?? "";
      try {
        const response = await fetch(`/web/tema?para=${saved[theme]}`,
          { credentials: "same-origin", cache: "no-store" });
        if (response.status !== 204) throw new Error(String(response.status));
      } catch {
        location.assign(themeLink.href);
        return;
      } finally {
        saving = false;
      }
      if (theme) root.dataset.theme = theme;
      else delete root.dataset.theme;
      themeLabel.textContent = `Tema: ${label[theme]}`;
      paintBar(theme);
    });
  }

  mobile.addEventListener("change", sync);
  // Back/forward can restore an open dialog from the page cache.
  window.addEventListener("pageshow", close);
  sync();
}
