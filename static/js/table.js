// Table settings: copy or share a new invite link (the link is also shown as plain text).
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const field = document.querySelector("[data-invite-url]");
  if (!field) return;
  const copy = document.querySelector("[data-copy-invite]");
  const share = document.querySelector("[data-share-invite]");
  const status = document.querySelector("[data-copy-status]");
  const tell = (text) => { if (status) status.textContent = text; };

  if (copy && navigator.clipboard && window.isSecureContext) {
    copy.hidden = false;
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(field.value); tell("Copied. Paste it into a message to one person."); }
      catch { field.select(); tell("Select the link and copy it."); }
    });
  }
  if (share && navigator.share) {
    share.hidden = false;
    share.addEventListener("click", () => {
      navigator.share({ title: "Join my Family Table", url: field.value }).catch(() => {});
    });
  }
  field.addEventListener("focus", () => field.select());
});
