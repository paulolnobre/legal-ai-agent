// The assistant's page, improved. Sending, pressing a button and withdrawing
// happen without leaving the page, and the answer appears as it is written.
// Each of these is also a plain form on the page, so if this file never
// loads, nothing is lost: the page then looks again by itself while waiting.
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

const HOME = "/web/assistente/";
const POLL_MS = 1500;
// The request states that mean an answer is still on its way.
const WAITING = new Set(["sending", "queued", "running"]);
// And those that keep the composer closed: one request at a time.
const BUSY = new Set(["sending", "queued", "running", "deferred"]);

const section = document.getElementById("conversa");
const composer = document.querySelector("form.composer");
const note = document.getElementById("aviso");

function say(text) {
  if (!note) return;
  note.textContent = text;
  note.hidden = !text;
}

const field = composer ? composer.querySelector("textarea") : null;
const send = composer ? composer.querySelector("button[type=submit]") : null;
let busy = !!(field && field.disabled);
let attachment = null;
let recordingActive = false;
let updateAttachmentControls = () => {};
let clearAttachment = () => {};
let disposeAttachment = () => {};
let uploadAttachment = async () => null;

// The send button is on only when there is something to send and no request
// is open. Without this script it is always on, and the form posts.
function syncSend() {
  if (send && field) send.disabled = busy || recordingActive || (!field.value.trim() && !attachment);
}

// The box grows with what is written, from one line to its own max-height
// (about eight lines), and scrolls beyond that.
function grow() {
  if (!field) return;
  field.style.height = "auto";
  const cap = parseFloat(getComputedStyle(field).maxHeight);
  field.style.height = (Number.isFinite(cap) ? Math.min(field.scrollHeight, cap)
                                              : field.scrollHeight) + "px";
}

// The list of actions: one request at a time, like the composer.
const actionsList = document.querySelector("details[data-actions]");

function syncActions() {
  if (!actionsList) return;
  for (const button of actionsList.querySelectorAll("form button[type=submit]")) {
    button.disabled = busy;
  }
}

function closeActions() {
  if (!actionsList) return;
  for (const open of actionsList.querySelectorAll("details[open]")) open.open = false;
  actionsList.open = false;
}

function setBusy(now) {
  busy = now;
  syncActions();
  if (field) field.disabled = now;
  syncSend();
  updateAttachmentControls();
}

let cursor = section ? section.dataset.cursor : "";
let polling = null;
let ended = false;
const APP_LOCK = !!document.querySelector('meta[name="app-lock"][content="1"]');
let leaving = false;
let checkingSession = false;

function faceId() {
  if (leaving) return;
  leaving = true;
  ended = true;
  disposeAttachment();
  clearAttachment();
  clearTimeout(polling);
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

function signedOut(marker = "ended") {
  // Once: the session is over, so nothing more is asked of the server.
  ended = true;
  disposeAttachment();
  clearAttachment();
  clearTimeout(polling);
  if (APP_LOCK && (marker === "locked"
      || (marker === "ended" && (window.matchMedia("(display-mode: standalone)").matches
                                 || navigator.standalone === true)))) {
    faceId();
    return;
  }
  setBusy(true);
  say("Sua sessão terminou. Peça um novo link no Telegram com /web.");
}

// Each turn is placed by its id: one seen again replaces itself, so an edit
// (a pressed button's result) shows, and nothing is ever shown twice. A turn
// not yet on the page goes where its id puts it, even one that arrives late.
function idOf(node) {
  return Number(node.id.slice(1));
}

function place(turn) {
  const list = section.querySelector("ol.talk");
  const same = document.getElementById(turn.id);
  const node = document.importNode(turn, true);
  if (same) { same.replaceWith(node); return; }
  const empty = document.getElementById("vazia");
  if (empty) empty.remove();
  const later = [...list.children].find((li) => idOf(li) > idOf(node));
  list.insertBefore(node, later || null);
  if (!later) node.scrollIntoView({ block: "nearest" });
}

// One look at a time: a second one asked for while the first is on its way
// runs after it, so an older answer never lands on top of a newer one.
let looking = false;
let lookAgain = false;

async function look() {
  if (ended) return;
  if (looking) { lookAgain = true; return; }
  looking = true;
  clearTimeout(polling);
  try {
    await lookOnce();
  } finally {
    looking = false;
    if (lookAgain && !ended) { lookAgain = false; look(); }
  }
}

async function lookOnce() {
  let response;
  let text;
  try {
    response = await fetch(`${HOME}conversa?desde=${encodeURIComponent(cursor)}`,
                           { credentials: "same-origin" });
    if (ended) return;
    if (response.status === 401) {
      ended = true;
      let marker = "ended";
      try { marker = (await response.text()).trim(); } catch (err) { /* ended still stops */ }
      if (!leaving) signedOut(marker);
      return;
    }
    if (!response.ok) { schedule(); return; }
    text = await response.text();
  } catch (err) {
    // Offline for a moment, or cut off mid-answer: look again.
    schedule();
    return;
  }
  if (ended) return;
  const doc = new DOMParser().parseFromString(text, "text/html");
  const fresh = doc.getElementById("novas");
  if (!fresh) { schedule(); return; }
  if (fresh.dataset.recarregar) {
    // More changed than one answer carries: the page itself, again.
    location.reload();
    return;
  }
  cursor = fresh.dataset.cursor || cursor;
  for (const turn of fresh.querySelectorAll("ol > li.turn")) place(turn);
  const state = doc.getElementById("estado");
  const current = document.getElementById("estado");
  if (state && current) current.replaceWith(document.importNode(state, true));
  const status = state ? state.dataset.estado : "nenhum";
  setBusy(BUSY.has(status));
  if (WAITING.has(status)) schedule();
}

function schedule() {
  clearTimeout(polling);
  if (!ended) polling = setTimeout(look, POLL_MS);
}

// What a form sends, read before anything in it is disabled: a disabled
// field is left out of the form, and the message would go empty.
function fields(form) {
  const body = new URLSearchParams(new FormData(form));
  body.set("via", "script");
  return body;
}

// One way to post any of the page's forms: the server answers the script
// with the note alone, and the page then asks what changed.
async function post(form, body) {
  let response;
  let text = "";
  try {
    response = await fetch(form.action, { method: "POST", body, credentials: "same-origin",
                                          redirect: "manual" });
    if ((response.headers.get("content-type") || "").startsWith("text/plain")) {
      text = await response.text();
    }
  } catch (err) {
    if (!ended) say("Não consegui falar com o servidor. Tente de novo.");
    return false;
  }
  // The session may have ended while this was on its way: that, said once,
  // is the last word.
  if (ended) return false;
  if (response.status === 401) { signedOut(text.trim() || "ended"); return false; }
  if (response.type === "opaqueredirect") { location.assign(HOME); return false; }
  if (response.ok) { say(text); return true; }
  say(text || "Recarregue a página e tente de novo.");
  return false;
}

// Raw-body uploads keep the original out of form parsers and disk spools.
// Nothing is stored in browser storage, and no original is fetched back.
(function attachments() {
  if (!composer || !field) return;
  const clip = composer.querySelector('[data-attach-toggle]');
  if (!clip) return;
  const choices = composer.querySelector('[data-attach-choices]');
  const photo = composer.querySelector('[data-photo-input]');
  const audio = composer.querySelector('[data-audio-input]');
  const microphone = composer.querySelector('[data-microphone]');
  // Dictation's bar: it takes the box's place while recording.
  const box = composer.querySelector('.box');
  const bar = composer.querySelector('[data-recording]');
  const cancel = composer.querySelector('[data-record-cancel]');
  const done = composer.querySelector('[data-record-done]');
  const clock = composer.querySelector('[data-record-clock]');
  const recordState = composer.querySelector('[data-record-state]');
  const wave = composer.querySelector('[data-wave]');
  const chip = composer.querySelector('[data-attachment-chip]');
  const label = composer.querySelector('[data-attachment-label]');
  const remove = composer.querySelector('[data-remove-attachment]');
  const archiveChoice = composer.querySelector('[data-archive-choice]');
  const archive = composer.querySelector('[data-archive]');
  const choosePhoto = composer.querySelector('[data-choose-photo]');
  const chooseAudio = composer.querySelector('[data-choose-audio]');
  // A document for the archive: on the page only for whom may put one there.
  const documentInput = composer.querySelector('[data-document-input]');
  const chooseDocument = composer.querySelector('[data-choose-document]');
  let recorder = null;
  let stream = null;
  let timer = null;
  let generation = 0;
  let microphonePending = false;
  let upload = null;
  let transcribing = false;
  let reading = null;
  let finishing = false;
  let audioContext = null;
  let frame = 0;
  const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    && window.MediaRecorder);

  function duration(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
  }

  updateAttachmentControls = () => {
    clip.disabled = busy || recordingActive;
    microphone.disabled = busy || microphonePending || transcribing;
    photo.disabled = audio.disabled = remove.disabled = busy || recordingActive;
    choosePhoto.disabled = chooseAudio.disabled = busy || recordingActive;
    if (documentInput && chooseDocument) {
      documentInput.disabled = chooseDocument.disabled = busy || recordingActive;
    }
    if (archive) archive.disabled = busy || recordingActive || !!(attachment && attachment.id);
  };

  function size(bytes) {
    if (bytes < 1024) return bytes + ' B';
    const kilo = bytes < 1024 * 1024;
    return (bytes / (kilo ? 1024 : 1024 * 1024))
      .toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + (kilo ? ' KB' : ' MB');
  }

  // A document's name and size, checked here only to spare a pointless
  // upload: the server checks both again, and its bytes. The words are the
  // server's own, carried on the input.
  function documentRefusal(file) {
    const dot = file.name.lastIndexOf('.');
    const extension = dot > 0 ? file.name.slice(dot).toLowerCase() : '';
    if (!documentInput.accept.split(',').includes(extension)) return documentInput.dataset.format;
    if (file.size > Number(documentInput.dataset.limit)) return documentInput.dataset.tooLarge;
    return '';
  }

  function choose(file, kind, seconds = null) {
    if (busy || !file) return;
    if (kind === 'document') {
      const refusal = documentRefusal(file);
      if (refusal) { say(refusal); documentInput.value = ''; return; }
    } else {
      const limit = kind === 'photo' ? 10 : kind === 'voice' ? 5 : 100;
      if (file.size > limit * 1024 * 1024) {
        say(kind === 'photo' ? 'A foto passa de 10 MB.' : kind === 'voice'
          ? 'A nota de voz passa de 5 MB.' : 'O áudio passa de 100 MB.');
        return;
      }
    }
    attachment = { file, kind, id: null };
    const name = { photo: 'Foto', voice: 'Nota de voz', audio: 'Áudio', document: 'Documento' }[kind];
    label.textContent = name + (kind === 'document' ? ' · ' + file.name + ' · ' + size(file.size)
      : seconds === null ? ' · ' + size(file.size) : ' ' + duration(seconds));
    chip.hidden = false;
    field.required = false;
    // A document always goes to the archive: the box is for photos and audio.
    if (archiveChoice) archiveChoice.hidden = kind === 'document';
    if (archive && kind === 'document') archive.checked = false;
    say('');
    syncSend();
    updateAttachmentControls();
  }

  clearAttachment = () => {
    attachment = null;
    photo.value = audio.value = '';
    if (documentInput) documentInput.value = '';
    field.required = true;
    chip.hidden = true;
    if (archiveChoice) archiveChoice.hidden = true;
    if (archive) archive.checked = false;
    syncSend();
    updateAttachmentControls();
  };

  function closeChoices() { choices.hidden = true; clip.setAttribute('aria-expanded', 'false'); }
  clip.hidden = false;
  microphone.hidden = !canRecord;
  clip.addEventListener('click', () => {
    choices.hidden = !choices.hidden;
    clip.setAttribute('aria-expanded', String(!choices.hidden));
  });
  choosePhoto.addEventListener('click', () => { closeChoices(); photo.click(); });
  chooseAudio.addEventListener('click', () => { closeChoices(); audio.click(); });
  if (documentInput && chooseDocument) {
    chooseDocument.addEventListener('click', () => { closeChoices(); documentInput.click(); });
    documentInput.addEventListener('change', () => choose(documentInput.files[0], 'document'));
  }
  photo.addEventListener('change', () => choose(photo.files[0], 'photo'));
  audio.addEventListener('change', () => choose(audio.files[0], 'audio'));
  remove.addEventListener('click', clearAttachment);

  // The bar in place of the box, or the box back.
  function showBar(on) {
    if (bar) bar.hidden = !on;
    if (box && box.classList) box.classList.toggle('is-recording', on);
    if (!on && recordState) recordState.hidden = true;
    if (clock) clock.hidden = !on;
    if (cancel) cancel.disabled = false;
    if (done) done.disabled = false;
  }

  // The voice's wave, drawn from the microphone as it records: bars that
  // scroll from the right, as the chats people know draw it.
  function drawWave(source) {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!wave || !Context || !wave.getContext) return;
    try {
      audioContext = new Context();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      audioContext.createMediaStreamSource(source).connect(analyser);
      const samples = new Uint8Array(analyser.fftSize);
      const levels = [];
      const paint = wave.getContext('2d');
      let last = 0;
      const tick = (now) => {
        frame = requestAnimationFrame(tick);
        const ratio = window.devicePixelRatio || 1;
        const width = wave.clientWidth * ratio, height = wave.clientHeight * ratio;
        if (wave.width !== width || wave.height !== height) { wave.width = width; wave.height = height; }
        if (now - last > 60) {
          last = now;
          analyser.getByteTimeDomainData(samples);
          let peak = 0;
          for (const sample of samples) peak = Math.max(peak, Math.abs(sample - 128) / 128);
          levels.push(Math.min(1, peak * 2.2));
        }
        // A row of dots the width of the bar from the start; what is said
        // comes in from the right as rounded bars, and scrolls left.
        const thick = 3 * ratio, step = 6 * ratio;
        const count = Math.max(1, Math.floor(width / step));
        while (levels.length < count) levels.unshift(0);
        while (levels.length > count) levels.shift();
        paint.clearRect(0, 0, width, height);
        paint.fillStyle = getComputedStyle(wave).color;
        levels.forEach((level, index) => {
          const tall = Math.max(thick, level * height * 0.9);
          const x = index * step + (width - count * step) / 2;
          const y = (height - tall) / 2;
          paint.beginPath();
          if (paint.roundRect) paint.roundRect(x, y, thick, tall, thick / 2);
          else paint.rect(x, y, thick, tall);
          paint.fill();
        });
      };
      frame = requestAnimationFrame(tick);
    } catch (error) {
      // No wave: the recording goes on, with its clock.
    }
  }

  function stop(discard = false) {
    if (discard) { generation += 1; finishing = false; }
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    if (stream) for (const track of stream.getTracks()) track.stop();
    stream = null;
    clearInterval(timer);
    timer = null;
    cancelAnimationFrame(frame);
    if (audioContext) { audioContext.close().catch(() => {}); audioContext = null; }
    microphonePending = false;
    recordingActive = false;
    if (discard) showBar(false);
    syncSend(); updateAttachmentControls();
  }

  // What was said, read by the server and put in the box after what is
  // there: the person reads it, corrects it and sends it as typed.
  async function transcribe(recording) {
    const mine = generation;
    reading = new AbortController();
    transcribing = true;
    if (recordState) recordState.hidden = false;
    if (clock) clock.hidden = true;
    if (done) done.disabled = true;
    updateAttachmentControls();
    try {
      const csrf = composer.querySelector('input[name=csrf]').value;
      const response = await fetch(HOME + 'ditado', {
        method: 'POST', credentials: 'same-origin', redirect: 'manual',
        headers: { 'X-CSRF': csrf }, body: recording, signal: reading.signal,
      });
      // Dropped while it was being read: what came back is not wanted.
      if (ended || mine !== generation) return;
      if (response.status === 401) { signedOut((await response.text()).trim()); return; }
      if (!response.ok) {
        const plain = (response.headers.get('content-type') || '').startsWith('text/plain');
        say(plain ? await response.text() : 'Não consegui transcrever agora. Tente de novo.');
        return;
      }
      const data = await response.json();
      if (mine !== generation) return;
      const text = typeof data.texto === 'string' ? data.texto.trim() : '';
      if (text) {
        const before = field.value.trim();
        field.value = before ? before + ' ' + text : text;
        say('');
      }
    } catch (error) {
      if (!ended && mine === generation) say('Não consegui falar com o servidor. Tente de novo.');
    } finally {
      reading = null;
      if (mine === generation) {
        transcribing = false;
        finishing = false;
        showBar(false);
      }
      grow(); syncSend(); updateAttachmentControls();
      if (field.focus) field.focus();
    }
  }

  async function startRecording() {
    if (busy || !canRecord || recordingActive || transcribing) return;
    closeChoices();
    const mine = ++generation;
    microphonePending = true; recordingActive = true;
    syncSend(); updateAttachmentControls();
    try {
      const granted = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (mine !== generation || ended || document.visibilityState !== 'visible') {
        for (const track of granted.getTracks()) track.stop();
        return;
      }
      stream = granted;
      const mime = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/ogg;codecs=opus']
        .find((type) => MediaRecorder.isTypeSupported(type));
      const active = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
      recorder = active;
      const chunks = [];
      let size = 0;
      const started = performance.now();
      active.addEventListener('dataavailable', (event) => {
        if (mine !== generation || !event.data.size) return;
        size += event.data.size;
        if (size > 5 * 1024 * 1024) {
          stop(true); say('A gravação passou de 5 MB. Dite em partes menores.'); return;
        }
        chunks.push(event.data);
      });
      active.addEventListener('error', () => {
        stop(true); say('Não consegui gravar agora. Tente de novo ou escreva a mensagem.');
      });
      active.addEventListener('stop', () => {
        if (recorder === active) recorder = null;
        if (mine !== generation || ended || !finishing) return;
        if (!chunks.length) { finishing = false; showBar(false); return; }
        transcribe(new Blob(chunks, { type: active.mimeType }));
      });
      active.start(500);
      microphonePending = false;
      if (clock) clock.textContent = '0:00';
      showBar(true);
      drawWave(stream);
      timer = setInterval(() => {
        const elapsed = (performance.now() - started) / 1000;
        if (clock) clock.textContent = duration(elapsed);
        if (elapsed >= 300) finish();
      }, 250);
      updateAttachmentControls();
    } catch (error) {
      if (mine !== generation || ended) return;
      stop(true);
      say(error.name === 'NotAllowedError' || error.name === 'SecurityError'
        ? 'O microfone está bloqueado para este app. Libere em Ajustes e tente de novo.'
        : 'Não consegui gravar agora. Tente de novo ou escreva a mensagem.');
    }
  }

  // The tick: stop, then read what was said into the box.
  function finish() {
    if (!recordingActive || finishing) return;
    finishing = true;
    stop();
  }

  // X: drop the recording, or the reading of it if the tick was pressed.
  function discard() {
    if (transcribing) {
      generation += 1;
      if (reading) reading.abort();
      transcribing = false;
      finishing = false;
      showBar(false);
      syncSend(); updateAttachmentControls();
      return;
    }
    if (recordingActive) stop(true);
  }

  microphone.addEventListener('click', startRecording);
  if (cancel) cancel.addEventListener('click', discard);
  if (done) done.addEventListener('click', finish);
  composer.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') discard();
  });

  disposeAttachment = () => {
    stop(true);
    if (upload) upload.abort();
  };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' && recordingActive && !finishing) stop(true);
  });
  window.addEventListener('pagehide', disposeAttachment);

  uploadAttachment = async () => {
    const selected = attachment;
    if (!selected || selected.id) return selected ? selected.id : null;
    upload = new AbortController();
    try {
      say('Enviando anexo…');
      const csrf = composer.querySelector('input[name=csrf]').value;
      const headers = { 'X-CSRF': csrf, 'X-Anexo-Tipo': selected.kind,
        'X-Arquivar': archive && archive.checked ? '1' : '0' };
      // A header carries no accents: the name travels percent-encoded.
      if (selected.kind === 'document') headers['X-Anexo-Nome'] = encodeURIComponent(selected.file.name);
      const response = await fetch(HOME + 'anexo', {
        method: 'POST', credentials: 'same-origin', redirect: 'manual', headers,
        body: selected.file, signal: upload.signal,
      });
      if (ended) return null;
      if (response.status === 401) { signedOut((await response.text()).trim()); return null; }
      if (!response.ok) {
        // The route's refusals are a sentence as plain text; anything else
        // (a page, a proxy's error) is never shown as it came.
        const plain = (response.headers.get('content-type') || '').startsWith('text/plain');
        say(plain ? await response.text() : 'Recarregue a página e tente de novo.');
        return null;
      }
      const data = await response.json();
      if (!data.anexo || selected !== attachment) return null;
      selected.id = data.anexo;
      return selected.id;
    } catch (error) {
      if (!ended) say('Não consegui falar com o servidor. Tente de novo.');
      return null;
    } finally { upload = null; updateAttachmentControls(); }
  };
  updateAttachmentControls();
})();

if (section && composer && field && send) {
  syncSend();
  grow();
  field.addEventListener("input", () => { syncSend(); grow(); });
  window.addEventListener("resize", grow);

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || recordingActive) return;
    if (!field.value.trim() && !attachment) { say("Escreva a mensagem antes de enviar."); return; }
    const body = fields(composer);
    setBusy(true);
    if (attachment) {
      const id = await uploadAttachment();
      if (!id) { if (!ended) setBusy(false); return; }
      body.set('anexo', id);
    }
    if (await post(composer, body)) {
      clearAttachment();
      field.value = "";
      grow();
      look();
    } else if (!ended) {
      setBusy(false);
    }
  });

  // Enter sends, Shift+Enter breaks the line, as in the chats people know.
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  // A button in a reply, and "Retirar": forms that appear and go as the
  // conversation changes, so they are caught where they bubble.
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form === composer ||
        !form.matches("form[action$='botao'], form[action$='retirar'], form[action$='acao']")) {
      return;
    }
    event.preventDefault();
    const body = fields(form);
    const fromList = !!(actionsList && actionsList.contains(form));
    for (const button of form.querySelectorAll("button")) button.disabled = true;
    // An action is a message: from the tap on, nothing else is sent until
    // the server says how it went, as the composer does.
    if (fromList) setBusy(true);
    if (await post(form, body)) {
      if (fromList) { form.reset(); closeActions(); }
      look();
    } else {
      if (fromList) setBusy(false);
      for (const button of form.querySelectorAll("button")) button.disabled = busy && fromList;
    }
  });

  if (actionsList) {
    // On the phone the open list is a sheet over the composer; without this
    // script it stays in the page's flow, where its own summary closes it.
    actionsList.classList.add("sheet");
    // Its button sits with the clip and the microphone; the pill above the
    // box is only for a page without this script.
    const toggle = composer ? composer.querySelector("[data-actions-toggle]") : null;
    if (toggle) {
      actionsList.classList.add("in-composer");
      toggle.hidden = false;
      toggle.addEventListener("click", () => { actionsList.open = !actionsList.open; });
      actionsList.addEventListener("toggle", () => {
        toggle.setAttribute("aria-expanded", String(actionsList.open));
      });
    }
    const close = document.createElement("button");
    close.type = "button";
    close.className = "quiet";
    close.dataset.actionsClose = "";
    close.textContent = "Fechar";
    close.addEventListener("click", closeActions);
    actionsList.querySelector(".action-list").prepend(close);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && actionsList.open) {
        closeActions();
        (toggle && !toggle.hidden ? toggle : actionsList.querySelector("summary")).focus();
      }
    });
    // What the person follows, asked again each time it opens: the worker
    // may have added or stopped one since the page was drawn.
    const follows = actionsList.querySelector("details[data-action=desacompanhar]");
    if (follows) {
      follows.addEventListener("toggle", async () => {
        if (!follows.open) return;
        try {
          const response = await fetch(HOME + "acompanhamentos", { credentials: "same-origin",
                                                                   redirect: "manual" });
          if (!response.ok) return;
          const doc = new DOMParser().parseFromString(await response.text(), "text/html");
          const fresh = doc.querySelector("[data-follows]");
          const current = follows.querySelector("[data-follows]");
          if (fresh && current) current.replaceWith(document.importNode(fresh, true));
          syncActions();
        } catch (err) {
          // What is on the page stays.
        }
      });
    }
    syncActions();
  }

  const status = document.getElementById("estado");
  if (status && WAITING.has(status.dataset.estado)) schedule();
}
