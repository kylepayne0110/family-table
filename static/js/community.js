// Community: a Save button can only be sent once per page (the server also ignores repeats).
"use strict";

document.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-save-form]");
  if (!form) return;
  if (form.dataset.sent) { event.preventDefault(); return; }
  form.dataset.sent = "1";
  const button = form.querySelector("button[type=submit]");
  if (button) { button.disabled = true; button.textContent = "Saving…"; }
  // If the browser comes back to this page (Back button), allow another try.
  window.addEventListener("pageshow", () => { delete form.dataset.sent; if (button) button.disabled = false; }, { once: true });
});
