// "Add ingredients": one dinner's checklist in a sheet, with a live "Add 3 items" button.
// Without JavaScript the same checklist is a normal page and form.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const dialog = document.querySelector("[data-checklist-dialog]");
  const sheetBody = dialog ? dialog.querySelector("[data-checklist-body]") : null;

  // Keep the button honest: "Add 1 item", "Add 3 items", disabled when nothing is checked.
  function sync(form) {
    if (!form) return;
    const count = form.querySelectorAll("input[name=ingredient]:checked").length;
    const button = form.querySelector("[data-checklist-submit]");
    const label = form.querySelector("[data-checklist-label]");
    if (label && !form.dataset.busy) label.textContent = `Add ${count} item${count === 1 ? "" : "s"}`;
    if (button) button.disabled = count === 0 || Boolean(form.dataset.busy);
  }
  const syncAll = (root = document) => root.querySelectorAll("[data-checklist-form]").forEach(sync);
  syncAll();

  document.addEventListener("change", (event) => {
    const form = event.target.closest("[data-checklist-form]");
    if (form) sync(form);
  });

  function say(form, text, isError = false) {
    const status = form.querySelector("[data-checklist-status]");
    if (!status) return;
    status.hidden = !text;
    status.textContent = text;
    status.classList.toggle("is-error", isError);
  }

  // Open the sheet for a dinner (phones get a bottom sheet; the link still works without JS).
  document.addEventListener("click", async (event) => {
    const link = event.target.closest("[data-checklist-open]");
    if (!link || !dialog || typeof dialog.showModal !== "function") return;
    event.preventDefault();
    sheetBody.innerHTML = '<p class="sheet__loading">Loading…</p>';
    dialog.showModal();
    try {
      const res = await fetch(link.href, { headers: { "X-Requested-With": "fetch" }, cache: "no-store" });
      if (res.redirected) { window.location.href = "/login"; return; }
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      sheetBody.innerHTML = data.html;
      syncAll(sheetBody);
    } catch {
      sheetBody.innerHTML = '<p class="sheet__loading">Couldn’t load the ingredients. Check the connection and try again.</p>';
    }
  });

  if (dialog) {
    dialog.addEventListener("click", (event) => {
      // Tapping the close button or the dimmed backdrop closes the sheet.
      if (event.target.closest("[data-sheet-close]") || event.target === dialog) dialog.close();
    });
  }

  // Send the checklist in the background. The form carries a request key, so if the
  // connection drops and the user taps again, the server still adds the items only once.
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-checklist-form]");
    if (!form) return;
    event.preventDefault();
    if (form.dataset.busy) return;            // a double tap while sending does nothing
    const submitter = event.submitter;
    const url = submitter && submitter.formAction ? submitter.formAction : form.action;
    const button = form.querySelector("[data-checklist-submit]");
    const label = form.querySelector("[data-checklist-label]");
    form.dataset.busy = "1";
    if (button) button.disabled = true;
    if (label && url === form.action) label.textContent = "Adding…";
    say(form, "");

    let res;
    try {
      res = await fetch(url, {
        method: "POST", body: new URLSearchParams(new FormData(form)),
        headers: { "X-Requested-With": "fetch" },
      });
    } catch {
      delete form.dataset.busy;
      sync(form);
      say(form, "Not added yet: no connection. Tap again when you're back online.", true);
      return;
    }
    if (res.redirected) { window.location.href = "/login"; return; }
    if (res.status === 403) {
      delete form.dataset.busy;
      say(form, "This page expired. Reload it and try again.", true);
      return;
    }
    let data = null;
    try { data = await res.json(); } catch { /* handled below */ }
    if (!data || res.status >= 500) {
      delete form.dataset.busy;
      sync(form);
      say(form, "Couldn't add those yet. Try again.", true);
      return;
    }
    // Fresh checklist from the server (new request key, "On the list" tags, new amounts).
    const holder = form.parentElement;
    holder.innerHTML = data.checklist;
    const fresh = holder.querySelector("[data-checklist-form]");
    sync(fresh);
    if (fresh) say(fresh, data.error || data.message || "", Boolean(data.error));
    document.dispatchEvent(new CustomEvent("meal:updated", { detail: data }));
  });
});
