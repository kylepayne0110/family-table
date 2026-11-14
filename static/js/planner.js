// Meal Planner: drag-and-drop between days (mouse and touch), plus background saving.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const el = document.querySelector("[data-live]");
  if (!el || !window.LiveRegion) return;

  let drag = null;  // the dinner being dragged, if any
  const live = new window.LiveRegion(el, {
    interval: 8000,
    canApply: () => !drag && !el.querySelector("details.move-menu[open]"),
  });
  live.start();

  const board = () => el.querySelector("[data-plan-board]");

  // Remove and the Move menu: save in the background, then show the server's board.
  el.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-plan-form]");
    if (!form) return;
    event.preventDefault();
    const menu = form.closest("details");
    if (menu) menu.open = false;   // an open menu would hold back the fresh board
    board().classList.add("is-saving");
    live.send(form);
  });

  // Swap what two day cards show right away, before the server confirms.
  function swapSlots(a, b) {
    const slotA = a.querySelector("[data-slot]");
    const slotB = b.querySelector("[data-slot]");
    const kidsA = Array.from(slotA.childNodes);
    slotA.replaceChildren(...slotB.childNodes);
    slotB.replaceChildren(...kidsA);
    const emptyA = a.classList.contains("is-empty");
    a.classList.toggle("is-empty", b.classList.contains("is-empty"));
    b.classList.toggle("is-empty", emptyA);
  }

  function sendMove(fromDay, toDay) {
    const token = el.querySelector("input[name=csrf_token]");
    const form = document.createElement("form");
    form.action = `/plan/${board().dataset.week}/move`;
    for (const [name, value] of [["csrf_token", token ? token.value : ""], ["from_day", fromDay], ["to_day", toDay]]) {
      const input = document.createElement("input");
      input.name = name;
      input.value = value;
      form.append(input);
    }
    board().classList.add("is-saving");
    live.send(form);
  }

  // Pointer Events cover mouse, touch, and pen with one code path.
  // A drag only starts after a small movement, so a plain tap on the handle opens the Move menu.
  el.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest("[data-drag-handle]");
    if (!handle || event.button > 0) return;
    event.preventDefault();
    const card = handle.closest("[data-day]");
    const start = { x: event.clientX, y: event.clientY };
    handle.setPointerCapture(event.pointerId);

    const begin = () => {
      const rect = card.getBoundingClientRect();
      const ghost = card.cloneNode(true);
      ghost.removeAttribute("id");
      ghost.classList.add("drag-ghost");
      ghost.setAttribute("aria-hidden", "true");
      Object.assign(ghost.style, { width: `${rect.width}px`, height: `${rect.height}px` });
      document.body.append(ghost);
      card.classList.add("is-drag-source");
      drag = { card, ghost, dx: start.x - rect.left, dy: start.y - rect.top, x: start.x, y: start.y, target: null };
      requestAnimationFrame(autoScroll);
    };

    const onMove = (e) => {
      if (!drag) {
        if (Math.hypot(e.clientX - start.x, e.clientY - start.y) < 6) return;
        begin();
      }
      drag.x = e.clientX;
      drag.y = e.clientY;
      position();
    };

    const finish = (e, cancelled) => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onCancel);
      if (!drag) {
        // A tap, not a drag: open the accessible Move menu instead.
        if (!cancelled) {
          const menu = card.querySelector("details.move-menu");
          if (menu) { menu.open = true; menu.querySelector("select").focus(); }
        }
        return;
      }
      const { target, ghost } = drag;
      ghost.remove();
      card.classList.remove("is-drag-source");
      el.querySelectorAll(".is-drop-target").forEach((c) => c.classList.remove("is-drop-target"));
      drag = null;
      if (!cancelled && target && target !== card) {
        swapSlots(card, target);
        sendMove(card.dataset.day, target.dataset.day);
      }
    };
    const onUp = (e) => finish(e, false);
    const onCancel = (e) => finish(e, true);
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onCancel);
  });

  // Move the floating copy and highlight the day under the finger or mouse.
  function position() {
    if (!drag) return;
    drag.ghost.style.transform = `translate(${drag.x - drag.dx}px, ${drag.y - drag.dy}px)`;
    const under = document.elementFromPoint(drag.x, drag.y);
    const target = under ? under.closest("[data-plan-board] [data-day]") : null;
    if (target !== drag.target) {
      if (drag.target) drag.target.classList.remove("is-drop-target");
      if (target && target !== drag.card) target.classList.add("is-drop-target");
      drag.target = target;
    }
  }

  // On phones the week is taller than the screen: scroll when dragging near an edge.
  function autoScroll() {
    if (!drag) return;
    const edge = 70;
    if (drag.y < edge) window.scrollBy(0, -12);
    else if (drag.y > window.innerHeight - edge) window.scrollBy(0, 12);
    position();
    requestAnimationFrame(autoScroll);
  }
});
