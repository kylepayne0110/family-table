// Shopping list: one-tap check-off, instant feedback, and live sync between phones.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const el = document.querySelector("[data-live]");
  if (!el || !window.LiveRegion) return;

  const live = new window.LiveRegion(el, {
    interval: 5000,
    // Don't swap in new HTML while someone has an item menu open or is typing in it.
    canApply: () => !el.querySelector("details.item-menu[open]") && !el.contains(document.activeElement?.closest("input, select, textarea")),
  });
  live.start();

  // Keep "8 items left to buy" honest while the server catches up.
  function adjustNeedCount(delta) {
    const count = el.querySelector("[data-need-count]");
    const label = el.querySelector("[data-need-label]");
    if (!count) return;
    const next = Math.max(0, Number(count.textContent) + delta);
    count.textContent = String(next);
    if (label) label.textContent = next === 1 ? "item" : "items";
  }

  // Every list button is a normal form (works without JS). With JS we save it in the
  // background and move the row out of the way immediately: no waiting, no dialogs.
  el.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-list-form]");
    if (!form) return;
    event.preventDefault();
    const menu = form.closest("details.item-menu");
    if (menu) menu.open = false;   // an open menu would hold back the fresh list
    const row = form.closest("[data-item]");
    if (row && form.dataset.leaves) {
      row.classList.add("is-leaving");
      setTimeout(() => { row.hidden = true; }, 180);
      if (form.dataset.leaves === "need") adjustNeedCount(-1);
    }
    live.send(form, { itemId: row ? row.dataset.item : null });
  });

  // Add an item: send it, clear the box, and keep the keyboard up for the next one.
  const addForm = document.querySelector("[data-add-item]");
  if (addForm) {
    addForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const name = addForm.elements.name;
      if (!name.value.trim()) { name.focus(); return; }
      live.send(addForm);
      name.value = "";
      addForm.elements.quantity.value = "";
      name.focus();
    });
  }
});
