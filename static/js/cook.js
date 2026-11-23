// Cook with me: one tap starts a two-way spoken conversation about this planned dinner.
// The server keeps the place in the recipe and answers; this file listens, speaks, and
// keeps the screen in step. See voice.js for listening and interruption handling.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const panel = document.querySelector("[data-cook]");
  const startButtons = document.querySelectorAll("[data-cook-start]");
  if (!panel || !startButtons.length || !window.FamilyVoice) return;

  const mealId = panel.dataset.mealId;
  const $ = (sel) => panel.querySelector(sel);
  const ui = {
    state: $("[data-cook-state]"), hint: $("[data-cook-hint]"), heard: $("[data-cook-heard]"), reply: $("[data-cook-reply]"),
    orb: $("[data-cook-orb]"), card: $("[data-cook-step-card]"), label: $("[data-cook-step-label]"),
    stepText: $("[data-cook-step-text]"), mute: $("[data-cook-mute]"), retry: $("[data-cook-retry]"),
    typeForm: $("[data-cook-type]"),
  };
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const tableId = document.querySelector('input[name="table_id"]')?.value || "";
  const storeKey = `ft-cook-${mealId}`;
  const support = window.FamilyVoice.VoiceIO.support();
  let steps = [];
  let step = Number(panel.dataset.step) || 0;
  let total = Number(panel.dataset.total) || 0;
  let turn = 0;
  let inflight = null;
  let heardProposal = null;     // a recipe change the phone finished reading aloud
  let lastFailed = null;
  let active = false;

  const STATES = {
    listening: ["Listening", "Talk any time. Tap the circle to interrupt."],
    hearing: ["Listening", "Go ahead, I'm listening."],
    thinking: ["Thinking…", "One moment."],
    speaking: ["Speaking", "Tap the circle (or just talk) to interrupt."],
    muted: ["Muted", "The microphone is off. Tap Unmute to talk, or type below."],
    paused: ["Paused", "Tap the circle to pick up where you left off."],
    "needs-tap": ["Tap to keep talking", "Listening stopped. Tap the circle to start again."],
    idle: ["Still here", "I stopped listening after a quiet spell. Tap the circle to talk."],
    blocked: ["Microphone off", "The microphone isn't allowed here. You can type below; replies are still spoken. To talk, allow the microphone for this site in your browser settings."],
    typing: ["Type to talk", "This browser can't listen, so type below. Replies are still spoken where possible."],
    off: ["", ""],
  };

  function setState(state) {
    panel.dataset.state = state;
    const [label, hint] = STATES[state] || ["", ""];
    ui.state.textContent = label;
    ui.hint.textContent = hint;
  }

  const io = new window.FamilyVoice.VoiceIO({
    silenceMs: 1100,
    onState: setState,
    onInterim: (text) => { ui.heard.textContent = `“${text}”`; },
    onUtterance: (text) => {
      if (text === "stop") { ui.reply.textContent = "Okay. Say “next” or ask me anything."; return; }
      send(text);
    },
    onSpeechStart: () => { if (inflight) { inflight.abort(); inflight = null; } },
    onProblem: (code) => {
      if (code === "network") ui.hint.textContent = "Speech recognition lost its connection. Trying again…";
    },
  });

  // ---- Showing the recipe position ----
  function showStep(n, list) {
    if (Array.isArray(list) && list.length) steps = list;
    step = n;
    total = steps.length || total;
    ui.card.hidden = !n;
    if (n) {
      ui.label.textContent = `Step ${n} of ${total}`;
      ui.stepText.textContent = steps[n - 1] || "";
    }
    document.querySelectorAll(".step.is-current").forEach((el) => el.classList.remove("is-current"));
    const target = document.getElementById(`step-${n}`);
    if (target) target.classList.add("is-current");
  }

  function applyPage(data) {
    // Keep the recipe view, serving selector, and typed conversation in step with what was said.
    const recipe = document.querySelector("[data-planned-recipe]");
    if (typeof data.recipe === "string" && recipe) recipe.innerHTML = data.recipe;
    const log = document.querySelector("[data-assistant-log]");
    if (typeof data.log === "string" && log) log.innerHTML = data.log;
    const servings = document.querySelector("[data-assistant-servings]");
    if (servings && data.servings) servings.textContent = String(data.servings);
    if (step) showStep(step);
  }

  // ---- Talking to the server ----
  const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : `k${Date.now()}${Math.random().toString(36).slice(2)}`);

  async function send(text, { typed = false, key = null } = {}) {
    const mine = ++turn;
    if (inflight) inflight.abort();          // a newer turn replaces an unanswered one
    const controller = new AbortController();
    inflight = controller;
    ui.heard.textContent = `“${text}”`;
    ui.retry.hidden = true;
    io.setThinking(true);
    const slow = setTimeout(() => { if (mine === turn) ui.hint.textContent = "Still thinking… the assistant can be slow on a busy computer."; }, 6000);
    const requestKey = key || newKey();     // a retry keeps its key, so the server never acts on it twice
    const body = new URLSearchParams({ csrf_token: csrf, table_id: tableId, text, request_key: requestKey });
    if (heardProposal) body.set("proposal_id", String(heardProposal));
    heardProposal = null;                    // a yes only counts right after hearing the change
    let data = null;
    try {
      const res = await fetch(`/plan/meals/${mealId}/cook`, { method: "POST", body, signal: controller.signal,
        headers: { "X-Requested-With": "fetch" } });
      if (res.redirected) { location.href = "/login"; return; }
      data = await res.json().catch(() => null);
      if (!res.ok) {
        if (data && data.access === "lost") { end(); ui.reply.textContent = "You no longer have access to this Table."; return; }
        throw new Error((data && data.error) || "failed");
      }
    } catch (error) {
      if (controller.signal.aborted) return;   // replaced by a newer turn: say nothing
      clearTimeout(slow);
      io.setThinking(false);
      lastFailed = { text, key: requestKey };
      ui.retry.hidden = false;
      const msg = error.message && error.message !== "failed" && error.message !== "Failed to fetch"
        ? error.message : "I lost the connection. Your place is saved; say that again when you're back online.";
      ui.reply.textContent = msg;
      if (mine === turn) io.speak(msg);
      return;
    } finally {
      clearTimeout(slow);
      if (inflight === controller) inflight = null;
    }
    if (mine !== turn || !data) return;       // an older answer that arrived late: never spoken
    io.setThinking(false);
    if (data.kind === "duplicate") return;
    lastFailed = null;
    showStep(data.step, data.steps);
    applyPage(data);
    ui.reply.textContent = data.show;
    try { sessionStorage.setItem(storeKey, "on"); } catch { /* ignore */ }
    const completed = await io.speak(data.say);
    // Only a change the cook actually heard all the way through can be confirmed by voice.
    if (completed && data.kind === "proposal" && data.proposal_id && mine === turn) heardProposal = data.proposal_id;
  }

  // ---- Starting, pausing, and ending ----
  async function syncState() {
    try {
      const res = await fetch(`/plan/meals/${mealId}/cook`, { headers: { "X-Requested-With": "fetch" }, cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      showStep(data.step, data.steps);
    } catch { /* offline: keep what's shown */ }
  }

  function open() {
    panel.hidden = false;
    document.body.classList.add("cook-open");
    panel.querySelector("[data-cook-orb]").focus({ preventScroll: true });
  }

  function start() {
    open();
    active = true;
    const greeting = panel.dataset.greeting || "How can I help?";
    ui.reply.textContent = greeting;
    ui.heard.textContent = "";
    // Spoken inside this tap: phones only allow speech and the microphone after a gesture.
    io.start(support.speak ? greeting : "");
    if (!support.listen) setState("typing");
    try { sessionStorage.setItem(storeKey, "on"); } catch { /* ignore */ }
    if (!support.listen) window.FamilyVoice.diag("speech_unavailable");
    syncState();
  }

  function end() {
    active = false;
    if (inflight) inflight.abort();
    io.end();
    panel.hidden = true;
    document.body.classList.remove("cook-open");
    try { sessionStorage.removeItem(storeKey); } catch { /* ignore */ }
  }

  startButtons.forEach((b) => b.addEventListener("click", (event) => { event.preventDefault(); start(); }));
  $("[data-cook-end]").addEventListener("click", end);
  ui.orb.addEventListener("click", () => {
    const state = panel.dataset.state;
    if (state === "speaking") { io.stopSpeaking(); return; }               // tap to interrupt
    if (["paused", "needs-tap", "idle"].includes(state)) {
      if (support.speak) io.speak("Okay, I'm listening.");   // inside the tap, so the phone allows speech again
      io.resume();
      return;
    }
    if (state === "muted") { toggleMute(); return; }
    if (state === "off" || !active) start();
  });
  function toggleMute() {
    const on = ui.mute.getAttribute("aria-pressed") !== "true";
    ui.mute.setAttribute("aria-pressed", String(on));
    ui.mute.querySelector("span").textContent = on ? "Unmute" : "Mute";
    io.mute(on);
  }
  ui.mute.addEventListener("click", toggleMute);
  ui.retry.addEventListener("click", () => { if (lastFailed) send(lastFailed.text, { key: lastFailed.key }); });
  panel.querySelectorAll("[data-cook-cue]").forEach((b) => b.addEventListener("click", () => {
    io.stopSpeaking();
    send(b.dataset.cookCue, { typed: true });
  }));
  ui.typeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const field = ui.typeForm.querySelector("input");
    const text = field.value.trim();
    if (!text) return;
    field.value = "";
    io.stopSpeaking();
    send(text, { typed: true });
  });

  // Screen lock, app switch, or leaving the page: release the microphone and stop talking.
  document.addEventListener("visibilitychange", () => {
    if (!active) return;
    if (document.hidden) io.pause();
    else { setState("paused"); syncState(); }
  });
  window.addEventListener("pagehide", () => { if (active) io.end(); });

  // Reopened (reload, back button, or the phone was locked): offer to pick up again with a tap.
  let wasActive = false;
  try { wasActive = sessionStorage.getItem(storeKey) === "on"; } catch { /* ignore */ }
  if (wasActive) {
    open();
    active = true;
    setState("paused");
    ui.reply.textContent = step ? `You were on step ${step} of ${total}. Tap the circle to keep going.` : "Tap the circle to keep talking.";
    syncState();
  }
  if (location.hash === "#cook" && !wasActive) open();
});
