// The integrations page, improved: while a Microsoft code is shown, ask the
// server now and then whether the person finished signing in, and show the
// result. Without this file the "Já autorizei" button does the same by hand.
// What this file writes is text (textContent). No eval, no inline handlers.

const card = document.querySelector("[data-ms-wait]");

if (card) {
  const interval = Math.max(5, Number(card.dataset.intervalo) || 5) * 1000;
  const expires = (Number(card.dataset.expira) || 0) * 1000;
  const status = card.querySelector("[data-ms-status]");
  let timer = null;

  const say = (text) => { if (status) status.textContent = text; };

  function next(delay) {
    clearTimeout(timer);
    // A tab in the background asks nothing; it asks again when it comes back.
    if (!document.hidden) timer = setTimeout(ask, delay);
  }

  async function ask() {
    if (document.hidden) return;
    if (Date.now() > expires) {
      say("O código expirou. Peça um novo código.");
      return;
    }
    const body = new URLSearchParams({ csrf: card.dataset.csrf || "", via: "script" });
    let outcome = "";
    try {
      const response = await fetch("/web/integracoes/microsoft/verificar",
        { method: "POST", body, credentials: "same-origin", redirect: "manual" });
      if (response.status === 401) { location.reload(); return; }
      outcome = response.ok ? (await response.text()).trim() : "";
    } catch (err) {
      outcome = "erro";
    }
    if (outcome === "conectada") { location.assign("/web/integracoes/?aviso=ms_conectada"); return; }
    if (outcome === "recusada" || outcome === "sem_codigo") {
      location.assign("/web/integracoes/?aviso=ms_" + outcome);
      return;
    }
    say(outcome === "erro" ? "A Microsoft não respondeu agora; tento de novo."
                           : "Aguardando você autorizar no site da Microsoft…");
    next(interval);
  }

  document.addEventListener("visibilitychange", () => next(1000));
  next(interval);
}
