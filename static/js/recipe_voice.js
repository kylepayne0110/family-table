// The recipe form: drafts that survive a reload, and "Tell me the recipe" (voice).
//
// The visible form is the source of truth. While someone talks, each finished utterance is
// sent with a snapshot of the form; the server answers with small edits that are applied
// here (and can be undone). Typing and talking mix freely. Questions are asked only after
// they've finished. Nothing in this file submits the form: Save is always the person's tap.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-recipe-form]");
  if (!form) return;
  const csrf = form.querySelector('input[name="csrf_token"]').value;
  const tableId = form.querySelector('input[name="table_id"]')?.value || "";
  const draftField = form.querySelector("[data-draft-id]");
  const statusEl = document.querySelector("[data-draft-status]");
  const editing = form.dataset.editing === "1";
  const recipeId = form.dataset.recipeId || "";
  let draftRev = Number(form.dataset.draftRev) || null;
  let voiceState = {};
  try { voiceState = JSON.parse(form.dataset.draftVoice || "{}") || {}; } catch { voiceState = {}; }

  const say = (text) => { if (statusEl) { statusEl.hidden = !text; statusEl.textContent = text; } };
  const post = async (url, data) => {
    const body = data instanceof FormData ? data : new URLSearchParams(data);
    if (!(data instanceof FormData)) { body.set("csrf_token", csrf); body.set("table_id", tableId); }
    const res = await fetch(url, { method: "POST", body, headers: { "X-Requested-With": "fetch" } });
    if (res.redirected) { location.href = "/login"; throw new Error("signed out"); }
    const json = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, json };
  };

  // ---- Reading and writing the form ----
  const field = (name) => form.querySelector(`[name="${name}"]`);
  const SCALARS = ["name", "description", "category", "cuisine", "card_tag", "creator_credit", "prep_minutes",
    "cook_minutes", "servings", "note_body", "note_author", "source_url", "imported_photo"];
  const ingRows = () => Array.from(form.querySelectorAll('[data-rows="ingredients"] [data-row]'));
  const stepRows = () => Array.from(form.querySelectorAll('[data-rows="steps"] [data-row]'));

  function snapshot() {
    const data = { editing };
    SCALARS.forEach((n) => { const el = field(n); if (el) data[n] = el.value; });
    data.ingredients = ingRows().map((r) => ({
      amount: r.querySelector('[name="ingredient_amount"]').value, name: r.querySelector('[name="ingredient_name"]').value,
    })).filter((r) => r.amount.trim() || r.name.trim());
    data.steps = stepRows().map((r) => ({
      title: r.querySelector('[name="step_title"]').value, body: r.querySelector('[name="step_body"]').value,
    })).filter((s) => s.title.trim() || s.body.trim());
    return data;
  }

  function addRow(kind) {
    form.querySelector(`[data-add-row="${kind}"]`)?.click();
    const rows = kind === "ingredients" ? ingRows() : stepRows();
    return rows[rows.length - 1];
  }

  function blankRow(kind) {
    const rows = kind === "ingredients" ? ingRows() : stepRows();
    return rows.find((r) => Array.from(r.querySelectorAll("input, textarea")).every((el) => !el.value.trim())) || addRow(kind);
  }

  const norm = (t) => (t || "").toLowerCase().replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();
  function findIngredient(match, index) {
    const rows = ingRows().filter((r) => r.querySelector('[name="ingredient_name"]').value.trim());
    const want = norm(match);
    return rows.find((r) => norm(r.querySelector('[name="ingredient_name"]').value) === want)
      || rows.find((r) => norm(r.querySelector('[name="ingredient_name"]').value).includes(want))
      || (Number.isInteger(index) ? rows[index] : null);
  }

  function flash(el) {
    if (!el) return;
    el.classList.remove("is-voice-filled");
    void el.offsetWidth;
    el.classList.add("is-voice-filled");
  }

  function restore(state) {
    SCALARS.forEach((n) => { const el = field(n); if (el && n in state) el.value = state[n]; });
    const fill = (kind, items, setter) => {
      let rows = kind === "ingredients" ? ingRows() : stepRows();
      while (rows.length < Math.max(items.length, 1)) { addRow(kind); rows = kind === "ingredients" ? ingRows() : stepRows(); }
      rows.forEach((row, i) => {
        if (i < items.length) setter(row, items[i]);
        else if (rows.length > Math.max(items.length, 1)) row.remove();
        else row.querySelectorAll("input, textarea").forEach((el) => { el.value = ""; });
      });
    };
    fill("ingredients", state.ingredients || [], (row, item) => {
      row.querySelector('[name="ingredient_amount"]').value = item.amount;
      row.querySelector('[name="ingredient_name"]').value = item.name;
    });
    fill("steps", state.steps || [], (row, item) => {
      row.querySelector('[name="step_title"]').value = item.title || "";
      row.querySelector('[name="step_body"]').value = item.body;
    });
    form.querySelectorAll("[data-step-number]").forEach((el, i) => { el.textContent = String(i + 1).padStart(2, "0"); });
  }

  const undoStack = [];
  const undoButton = document.querySelector("[data-rvoice-undo]");
  function pushUndo() {
    undoStack.push(snapshot());
    if (undoStack.length > 30) undoStack.shift();
    if (undoButton) undoButton.disabled = false;
  }
  function undo() {
    const last = undoStack.pop();
    if (!last) return false;
    restore(last);
    if (undoButton) undoButton.disabled = undoStack.length === 0;
    scheduleSave();
    return true;
  }

  const LABELS = { name: "name", servings: "servings", creator_credit: "created by", prep_minutes: "prep time",
    cook_minutes: "cook time", note_body: "note", category: "category", description: "description" };

  function applyOps(ops) {
    if (!ops || !ops.length) return [];
    const changed = [];
    if (ops.some((op) => op.op !== "undo" && op.op !== "mark_ai")) pushUndo();
    ops.forEach((op) => {
      if (op.op === "undo") { if (undo()) changed.push("undid the last change"); return; }
      if (op.op === "mark_ai") { document.querySelector("[data-ai-voice-notice]")?.removeAttribute("hidden"); return; }
      if (op.op === "set") {
        const el = field(op.field);
        if (!el) return;
        el.value = op.value;
        flash(el);
        changed.push(`${LABELS[op.field] || op.field}: ${op.value}`);
      } else if (op.op === "add_ingredient") {
        const row = blankRow("ingredients");
        row.querySelector('[name="ingredient_amount"]').value = op.amount || "";
        row.querySelector('[name="ingredient_name"]').value = op.name;
        flash(row);
        changed.push(`${op.amount ? op.amount + " " : ""}${op.name}`);
      } else if (op.op === "update_ingredient") {
        const row = findIngredient(op.match, op.index);
        if (!row) return;
        if (op.amount !== undefined) row.querySelector('[name="ingredient_amount"]').value = op.amount;
        if (op.name) row.querySelector('[name="ingredient_name"]').value = op.name;
        flash(row);
        changed.push(`${op.match}: ${op.amount}`);
      } else if (op.op === "remove_ingredient") {
        const row = findIngredient(op.match, op.index);
        if (!row) return;
        if (ingRows().length > 1) row.remove(); else row.querySelectorAll("input").forEach((el) => { el.value = ""; });
        changed.push(`removed ${op.match}`);
      } else if (op.op === "add_step") {
        const row = blankRow("steps");
        row.querySelector('[name="step_body"]').value = op.body;
        flash(row);
        changed.push(`step: ${op.body}`);
      }
    });
    scheduleSave();
    return changed;
  }

  // ---- Drafts: saved as you go, recoverable after a reload or a lost connection ----
  let saveTimer = null;
  let saving = false;
  let changedSinceLoad = Boolean(draftField && draftField.value);

  function scheduleSave(delay = 1200) {
    changedSinceLoad = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDraft, delay);
  }

  async function saveDraft() {
    if (saving) { scheduleSave(800); return; }
    if (!changedSinceLoad) return;
    saving = true;
    const fields = JSON.stringify(snapshot());
    const voice = JSON.stringify(voiceState);
    try {
      let result;
      if (!draftField.value) {
        result = await post("/recipes/drafts", { fields, voice, recipe_id: recipeId });
        if (result.ok) { draftField.value = String(result.json.id); draftRev = result.json.rev; }
      } else {
        result = await post(`/recipes/drafts/${draftField.value}`, { fields, voice, rev: draftRev ?? "" });
        if (result.ok) draftRev = result.json.rev;
        else if (result.status === 409 || result.status === 404) {
          // Another tab or phone changed this draft: keep both by starting a new one from this page.
          const fresh = await post("/recipes/drafts", { fields, voice, recipe_id: recipeId });
          if (fresh.ok) {
            draftField.value = String(fresh.json.id);
            draftRev = fresh.json.rev;
            say("This draft was also open somewhere else, so your changes here were saved as a separate draft.");
            return;
          }
        }
      }
      say(result && result.ok ? "Draft saved on your Family Table (not added to recipes until you press Save)." :
        "Couldn't save the draft just now. Your work is still on this page.");
    } catch {
      say("Offline: your draft will be saved when the connection is back.");
      scheduleSave(5000);
    } finally {
      saving = false;
    }
  }

  form.addEventListener("input", () => scheduleSave());
  form.addEventListener("change", () => scheduleSave());
  form.addEventListener("click", (event) => { if (event.target.closest("[data-remove-row]")) scheduleSave(); });
  window.addEventListener("online", () => { if (changedSinceLoad) scheduleSave(200); });
  form.addEventListener("submit", () => { clearTimeout(saveTimer); });

  // A chosen photo is kept with the draft (only you can see it until the recipe is saved).
  const photoInput = form.querySelector("[data-photo-input]");
  const importedField = form.querySelector("[data-imported-photo]");
  if (photoInput && importedField) {
    photoInput.addEventListener("change", async () => {
      const file = photoInput.files && photoInput.files[0];
      if (!file) return;
      const data = new FormData();
      data.append("csrf_token", csrf);
      data.append("table_id", tableId);
      data.append("photo", file);
      try {
        const result = await post("/recipes/photo", data);
        if (result.ok) { importedField.value = result.json.filename; scheduleSave(200); }
      } catch { /* the photo still goes with the form when you press Save */ }
    });
  }

  // ---- Voice ----
  const startButton = document.querySelector("[data-recipe-voice-start]");
  const panel = document.querySelector("[data-recipe-voice]");
  if (!startButton || !panel || !window.FamilyVoice) return;
  const support = window.FamilyVoice.VoiceIO.support();
  startButton.hidden = false;
  document.querySelector("[data-recipe-voice-note]")?.removeAttribute("hidden");
  const ui = {
    state: panel.querySelector("[data-rvoice-state]"), hint: panel.querySelector("[data-rvoice-hint]"),
    heard: panel.querySelector("[data-rvoice-heard]"), reply: panel.querySelector("[data-rvoice-reply]"),
    filled: panel.querySelector("[data-rvoice-filled]"), mute: panel.querySelector("[data-rvoice-mute]"),
    orb: panel.querySelector("[data-rvoice-orb]"),
  };
  const STATES = {
    listening: ["Listening", "Take your time. I'll fill in the form as you talk."],
    hearing: ["Listening", "Go on, I'm writing it down."],
    thinking: ["Writing…", ""],
    speaking: ["Speaking", "Just talk to interrupt me."],
    muted: ["Muted", "The microphone is off. You can keep typing in the form."],
    paused: ["Paused", "Tap the circle to keep going. Your draft is saved."],
    "needs-tap": ["Tap to keep talking", "Listening stopped. Tap the circle to start again."],
    idle: ["Still here", "Tap the circle when you want to talk again."],
    blocked: ["Microphone off", "The microphone isn't allowed here. You can type in the form; to talk, allow the microphone for this site in your browser settings."],
    typing: ["Can't listen here", "This browser can't listen. You can type in the form."],
    off: ["", ""],
  };
  const setState = (state) => {
    panel.dataset.state = state;
    const [label, hint] = STATES[state] || ["", ""];
    ui.state.textContent = label;
    if (hint) ui.hint.textContent = hint;
  };

  let active = false;
  let queue = Promise.resolve();
  let quietTimer = null;
  const QUIET_MS = 4000;

  const io = new window.FamilyVoice.VoiceIO({
    silenceMs: 1600,      // a little longer: people pause mid-recipe to think
    onState: setState,
    onInterim: (text) => { ui.heard.textContent = `“${text}”`; clearTimeout(quietTimer); },
    onUtterance: (text) => { if (text !== "stop") enqueue("fill", text); },
  });

  function enqueue(mode, text = "") {
    clearTimeout(quietTimer);
    queue = queue.then(() => turn(mode, text)).catch(() => {});
  }

  async function turn(mode, text) {
    if (!active) return;
    if (text) ui.heard.textContent = `“${text}”`;
    let result;
    try {
      result = await post("/recipes/voice", { mode, text, form: JSON.stringify(snapshot()), voice: JSON.stringify(voiceState) });
    } catch {
      ui.reply.textContent = "I lost the connection. What you said so far is in the form; keep typing, or try again when you're back online.";
      return;
    }
    if (!result.ok) {
      if (result.json && result.json.access === "lost") { end(); say("You no longer have access to this Table."); return; }
      ui.reply.textContent = (result.json && result.json.error) || "Something went wrong. Your form is unchanged.";
      return;
    }
    const data = result.json;
    voiceState = data.voice || voiceState;
    const changed = applyOps(data.ops);
    if (changed.length) ui.filled.textContent = `Filled in: ${changed.join("; ")}`;
    if (data.say) {
      ui.reply.textContent = data.say;
      await speakWhenQuiet(data.say);
    }
    // While they're still describing the recipe, wait for a real pause before asking anything.
    if (mode === "fill" && !data.say && !(voiceState.pending)) {
      clearTimeout(quietTimer);
      quietTimer = setTimeout(() => { if (active && panel.dataset.state === "listening") enqueue("next"); }, QUIET_MS);
    }
    scheduleSave(300);
  }

  async function speakWhenQuiet(text) {
    // Never talk over someone mid-sentence.
    for (let i = 0; i < 40 && panel.dataset.state === "hearing"; i += 1) await new Promise((r) => setTimeout(r, 250));
    if (panel.dataset.state === "hearing") return;
    await io.speak(text);
  }

  function start() {
    panel.hidden = false;
    document.body.classList.add("cook-open");
    active = true;
    const opening = snapshot();
    const greeting = opening.name || opening.ingredients.length
      ? "I can see what's already in the form. Tell me anything to add or change."
      : "Tell me about the recipe: what it's called, what goes in it, and how you make it. Take your time.";
    ui.reply.textContent = greeting;
    io.start(support.speak ? greeting : "");
    if (!support.listen) window.FamilyVoice.diag("speech_unavailable");
    scheduleSave(100);      // starting voice keeps what's already in the form, photo included
  }

  function end() {
    active = false;
    clearTimeout(quietTimer);
    io.end();
    panel.hidden = true;
    document.body.classList.remove("cook-open");
    scheduleSave(100);
  }

  startButton.addEventListener("click", start);
  panel.querySelector("[data-rvoice-end]").addEventListener("click", end);
  panel.querySelector("[data-rvoice-done]").addEventListener("click", () => { io.stopSpeaking(); enqueue("next"); });
  undoButton?.addEventListener("click", () => { if (undo()) ui.filled.textContent = "Undid the last change."; });
  ui.mute.addEventListener("click", () => {
    const on = ui.mute.getAttribute("aria-pressed") !== "true";
    ui.mute.setAttribute("aria-pressed", String(on));
    ui.mute.querySelector("span").textContent = on ? "Unmute" : "Mute";
    io.mute(on);
  });
  ui.orb.addEventListener("click", () => {
    const state = panel.dataset.state;
    if (state === "speaking") io.stopSpeaking();
    else if (["paused", "needs-tap", "idle"].includes(state)) { if (support.speak) io.speak("Okay, go ahead."); io.resume(); }
  });
  document.addEventListener("visibilitychange", () => {
    if (!active) return;
    if (document.hidden) { io.pause(); saveDraft(); } else setState("paused");
  });
  window.addEventListener("pagehide", () => { if (active) io.end(); });
});
