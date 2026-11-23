// Family Table: small progressive enhancements. Every page still works without JS.
"use strict";

// Show today's date in the visitor's own time zone.
function showLocalDate() {
  document.querySelectorAll("[data-local-date]").forEach((el) => {
    el.textContent = new Date().toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric",
    });
  });
}

// Safe wrappers: storage can be blocked in private windows.
function readStore(key) {
  try { return JSON.parse(sessionStorage.getItem(key) || "[]"); } catch { return []; }
}
function writeStore(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
}

// Ingredient checklist: live "x of y ready" counter, remembered for this tab.
function setupIngredients() {
  const list = document.querySelector("[data-ingredients]");
  const counter = document.querySelector("[data-ingredient-count]");
  if (!list) return;

  const boxes = Array.from(list.querySelectorAll("input[type=checkbox]"));
  const storeKey = "ft-checked-" + list.dataset.ingredients;

  // Restore checks if the page reloads mid-cook (e.g. phone went to sleep).
  readStore(storeKey).forEach((i) => { if (boxes[i]) boxes[i].checked = true; });

  const update = () => {
    const checked = boxes.filter((b) => b.checked).length;
    if (counter) counter.textContent = `${checked} of ${boxes.length} ready`;
    writeStore(storeKey, boxes.map((b, i) => (b.checked ? i : -1)).filter((i) => i >= 0));
  };

  list.addEventListener("change", update);
  update();
}

// Repeating form rows (ingredients and steps).
function setupRows() {
  const renumber = (container) => {
    container.querySelectorAll("[data-step-number]").forEach((el, i) => {
      el.textContent = String(i + 1).padStart(2, "0");
    });
  };

  document.querySelectorAll("[data-add-row]").forEach((button) => {
    const name = button.dataset.addRow;
    const container = document.querySelector(`[data-rows="${name}"]`);
    const template = document.querySelector(`[data-row-template="${name}"]`);
    if (!container || !template) return;

    button.addEventListener("click", () => {
      const row = template.content.firstElementChild.cloneNode(true);
      container.appendChild(row);
      renumber(container);
      row.querySelector("input, textarea").focus();
    });
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-row]");
    if (!button) return;
    const row = button.closest("[data-row]");
    const container = row.parentElement;

    // Keep at least one row; clear it instead of removing it.
    if (container.querySelectorAll("[data-row]").length > 1) {
      row.remove();
    } else {
      row.querySelectorAll("input, textarea").forEach((f) => { f.value = ""; });
    }
    renumber(container);
  });
}

// Preview a chosen photo before uploading.
function setupPhotoPreview() {
  const input = document.querySelector("[data-photo-input]");
  const preview = document.querySelector("[data-photo-preview]");
  if (!input || !preview) return;

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file || !file.type.startsWith("image/")) return;
    const img = document.createElement("img");
    img.alt = "Preview of the selected photo";
    img.src = URL.createObjectURL(file);
    img.onload = () => URL.revokeObjectURL(img.src);
    preview.replaceChildren(img);
  });
}

// Ask before destructive actions (forms marked with data-confirm).
function setupConfirms() {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
}

// "+ Add a note" opens and closes the note form.
function setupNoteToggle() {
  const toggle = document.querySelector("[data-note-toggle]");
  const form = document.getElementById("note-form");
  if (!toggle || !form) return;

  if (form.classList.contains("js-collapsed")) form.classList.add("is-collapsed");
  toggle.setAttribute("aria-expanded", String(!form.classList.contains("is-collapsed")));

  toggle.addEventListener("click", (event) => {
    event.preventDefault();
    const opening = form.classList.toggle("is-collapsed") === false;
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening) form.querySelector("textarea").focus();
  });
}

// Import from a website: show a loading state while the server fetches the page.
function setupImportForm() {
  const form = document.querySelector("[data-import-form]");
  const loading = document.querySelector("[data-import-loading]");
  if (!form) return;
  const button = form.querySelector("button[type=submit]");
  const label = button.textContent;

  form.addEventListener("submit", () => {
    button.disabled = true;
    button.textContent = "Importing\u2026";
    if (loading) loading.hidden = false;
  });

  // Reset if the browser restores this page from its back/forward cache.
  window.addEventListener("pageshow", () => {
    button.disabled = false;
    button.textContent = label;
    if (loading) loading.hidden = true;
  });
}

// Instant search on the recipe picker and staples list (the form still works without JS).
function setupFilters() {
  document.querySelectorAll("[data-filter-input]").forEach((input) => {
    const name = input.dataset.filterInput;
    const list = document.querySelector(`[data-filter-list="${name}"]`);
    const empty = document.querySelector(`[data-filter-empty="${name}"]`);
    if (!list) return;
    const filter = () => {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      list.querySelectorAll("[data-filter-text]").forEach((item) => {
        const match = words.every((w) => item.dataset.filterText.includes(w));
        item.hidden = !match;
        if (match) shown += 1;
      });
      if (empty) empty.hidden = shown > 0;
    };
    input.addEventListener("input", filter);
  });
}

// True when opened from the home-screen icon (no Safari address bar, back, or reload buttons).
function isInstalledApp() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

// The installed iPhone app keeps its own sign-in, separate from Safari: suggest staying signed in.
function setupInstalledApp() {
  if (!isInstalledApp()) return;
  const remember = document.querySelector('.login-card input[name="remember"]');
  if (remember) remember.checked = true;
}

// Error pages offer an in-page Back button (the installed app has no browser back button).
function setupBackButtons() {
  document.querySelectorAll("[data-history-back]").forEach((button) => {
    if (window.history.length < 2) return;
    button.hidden = false;
    button.addEventListener("click", () => window.history.back());
  });
}

// The service worker only shows a friendly screen when the server can't be reached.
// Browsers allow it on HTTPS sites and on localhost.
function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { /* the app works without it */ });
  });
}

// Move focus to a form's error summary so screen readers announce it.
function focusErrors() {
  const summary = document.querySelector("[data-focus-on-load]");
  if (summary) summary.focus();
}

// The Table switcher: close it when tapping elsewhere, and never carry this Table's unsaved
// ticks, chat drafts, or recipe drafts kept in this tab into the next Table.
function setupTableSwitch() {
  const menu = document.querySelector("[data-table-switch]");
  if (!menu) return;
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.open) { menu.open = false; menu.querySelector("summary").focus(); }
  });
  menu.querySelectorAll("[data-table-switch-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      try {
        Object.keys(sessionStorage).filter((k) => k.startsWith("ft-")).forEach((k) => sessionStorage.removeItem(k));
      } catch { /* storage blocked */ }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupTableSwitch();
  showLocalDate();
  setupIngredients();
  setupRows();
  setupPhotoPreview();
  setupConfirms();
  setupNoteToggle();
  setupImportForm();
  setupFilters();
  focusErrors();
  setupInstalledApp();
  setupBackButtons();
});

registerServiceWorker();
