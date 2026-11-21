// A planned dinner: serving selector and Cooking Assistant. Every control is a normal form
// (works without JavaScript); here they're sent in the background and the server's fresh
// HTML is swapped in, so the recipe updates without losing your place.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const page = document.querySelector("[data-meal]");
  if (!page) return;
  const mealId = page.dataset.meal;
  const recipe = document.querySelector("[data-planned-recipe]");
  const status = document.querySelector("[data-meal-status]");
  const panel = document.querySelector("[data-assistant]");
  const log = document.querySelector("[data-assistant-log]");
  const chatForm = document.querySelector("[data-assistant-form]");
  const input = chatForm ? chatForm.querySelector("textarea") : null;
  const note = document.querySelector("[data-assistant-note]");
  const servingsLabel = document.querySelector("[data-assistant-servings]");
  const draftKey = `ft-chat-draft-${mealId}`;

  const say = (text) => {
    if (!status) return;
    status.hidden = !text;
    status.textContent = text;
  };
  const tell = (text) => {
    if (!note) return;
    note.hidden = !text;
    note.textContent = text;
  };

  // Apply a server response: new recipe amounts, conversation, and serving count.
  function apply(data) {
    if (!data) return;
    if (typeof data.recipe === "string" && recipe) {
      // Keep the cook's ticks on the ingredient list (same positions) across a re-render.
      const ticked = Array.from(recipe.querySelectorAll(".ingredient__input"), (b) => b.checked);
      recipe.innerHTML = data.recipe;
      recipe.querySelectorAll(".ingredient__input").forEach((b, i) => { b.checked = Boolean(ticked[i]); });
    }
    if (typeof data.log === "string" && log) {
      log.innerHTML = data.log;
      scrollLog();
    }
    if (servingsLabel && data.servings) servingsLabel.textContent = String(data.servings);
  }

  async function post(url, body) {
    const res = await fetch(url, { method: "POST", body, headers: { "X-Requested-With": "fetch" } });
    if (res.redirected) { window.location.href = "/login"; return null; }
    if (res.status === 403) throw new Error("expired");
    const data = await res.json().catch(() => null);
    if (!data) throw new Error("bad response");
    return data;
  }

  // ---- Serving selector ----
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-servings-form]");
    if (!form) return;
    event.preventDefault();
    const body = new URLSearchParams(new FormData(form));
    if (event.submitter && event.submitter.name) body.set(event.submitter.name, event.submitter.value);
    form.classList.add("is-saving");
    try {
      const data = await post(form.action, body);
      if (!data) return;
      apply(data);
      say(data.error || "");
    } catch (error) {
      say(error.message === "expired" ? "This page expired. Reload it and try again."
                                      : "Couldn't change servings: check the connection and try again.");
    } finally {
      form.classList.remove("is-saving");
    }
  });
  document.addEventListener("change", (event) => {
    const box = event.target.closest("[data-servings-input]");
    if (box && box.form && box.checkValidity()) box.form.requestSubmit();
  });

  // Adding groceries doesn't change the recipe, but the assistant may mention it.
  document.addEventListener("meal:updated", (event) => {
    if (event.detail && typeof event.detail.log === "string" && log) log.innerHTML = event.detail.log;
  });

  // ---- Cooking Assistant panel ----
  if (!panel || !chatForm) return;
  let scrollBefore = 0;

  function scrollLog() {
    if (log) log.scrollTop = log.scrollHeight;
  }

  function openPanel(focus = true) {
    scrollBefore = window.scrollY;
    panel.classList.add("is-open");
    document.body.classList.add("assistant-open");
    if (window.location.hash !== "#assistant") history.replaceState(null, "", "#assistant");
    scrollLog();
    if (focus && input && window.matchMedia("(hover: hover)").matches) input.focus();
  }

  // Back to the recipe exactly where you were.
  function closePanel() {
    panel.classList.remove("is-open");
    document.body.classList.remove("assistant-open");
    history.replaceState(null, "", window.location.pathname + window.location.search);
    window.scrollTo(0, scrollBefore);
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-assistant-open]")) { event.preventDefault(); openPanel(); }
    else if (event.target.closest("[data-assistant-close]")) { event.preventDefault(); closePanel(); }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && panel.classList.contains("is-open")) closePanel();
  });
  // Reopen after a reload (the phone locked, or the page was left and reopened).
  if (window.location.hash === "#assistant") openPanel(false);

  // Keep an unsent question if the phone locks or the page reloads.
  try { if (input && !input.value) input.value = sessionStorage.getItem(draftKey) || ""; } catch { /* storage blocked */ }
  const saveDraft = () => { try { sessionStorage.setItem(draftKey, input.value); } catch { /* ignore */ } };
  if (input) {
    input.addEventListener("input", saveDraft);
    // Enter sends; Shift+Enter makes a new line.
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        chatForm.requestSubmit();
      }
    });
  }

  const newKey = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID()
    : `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`);
  const keyField = chatForm.elements.request_key;
  let sending = false;

  function bubble(text, role) {
    const div = document.createElement("div");
    div.className = `chat-msg chat-msg--${role} is-pending`;
    const p = document.createElement("p");
    p.className = "chat-msg__body";
    p.textContent = text;
    div.append(p);
    log.append(div);
    scrollLog();
    return div;
  }

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending) return;
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    sending = true;
    tell("");
    log.querySelector(".chat-empty")?.remove();
    const mine = bubble(text, "user");
    const thinking = bubble("Thinking…", "assistant");
    const body = new URLSearchParams(new FormData(chatForm));
    try {
      const data = await post(chatForm.action, body);
      if (!data) return;
      apply(data);
      if (data.error) tell(data.error);
      input.value = "";
      saveDraft();
      keyField.value = newKey();    // the next message gets its own key
    } catch (error) {
      // Same key on the retry: if the server did get it, it won't be answered twice.
      mine.remove();
      thinking.remove();
      tell(error.message === "expired" ? "This page expired. Reload it and try again."
                                       : "Not sent: no connection. Your question is still here; tap send to try again.");
    } finally {
      sending = false;
    }
  });

  // Confirm / Cancel on a proposed recipe change.
  log.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-chat-form]");
    if (!form) return;
    event.preventDefault();
    form.closest(".proposal__actions")?.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      apply(await post(form.action, new URLSearchParams(new FormData(form))));
    } catch {
      tell("Couldn't save that choice: check the connection and try again.");
      form.closest(".proposal__actions")?.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  });

  // A reply may have finished while the phone was locked: fetch the saved conversation.
  document.addEventListener("visibilitychange", async () => {
    if (document.hidden || sending) return;
    try {
      const res = await fetch(`/plan/meals/${mealId}/chat`, { headers: { "X-Requested-With": "fetch" }, cache: "no-store" });
      if (res.ok && !res.redirected) apply(await res.json());
    } catch { /* offline: keep what's shown */ }
  });

  // ---- Voice input (where the browser supports it) ----
  const mic = chatForm.querySelector("[data-assistant-mic]");
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!mic || !Recognition) return;   // no speech support: typing still works
  mic.hidden = false;
  let listening = null;

  mic.addEventListener("click", () => {
    if (listening) { listening.stop(); return; }
    const rec = new Recognition();
    rec.lang = document.documentElement.lang === "en" ? "en-US" : document.documentElement.lang;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    const before = input.value ? `${input.value.trim()} ` : "";
    rec.onresult = (e) => {
      input.value = before + Array.from(e.results, (r) => r[0].transcript).join(" ");
      saveDraft();
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        tell("Microphone access is off, so voice input isn't available. You can keep typing.");
      } else if (e.error === "no-speech") {
        tell("I didn't hear anything. Tap the microphone and try again, or type your question.");
      } else if (e.error !== "aborted") {
        tell("Voice input isn't working right now. You can keep typing.");
      }
    };
    rec.onend = () => {
      listening = null;
      if (note && note.textContent.startsWith("Listening")) tell("");
      mic.classList.remove("is-listening");
      mic.setAttribute("aria-pressed", "false");
      input.focus();
    };
    try {
      rec.start();
      listening = rec;
      mic.classList.add("is-listening");
      mic.setAttribute("aria-pressed", "true");
      tell("Listening… tap the microphone again to stop.");
    } catch {
      tell("Voice input isn't available here. You can keep typing.");
    }
  });
});
