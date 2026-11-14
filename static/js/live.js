// Family Table: keeps a server-rendered part of the page in sync between phones.
// The server is the source of truth. This file only sends changes and swaps in fresh HTML.
"use strict";

class LiveRegion {
  // el has data-live-url (polling endpoint) and data-version (what we're showing now).
  constructor(el, { interval = 6000, canApply = () => true, onApply = () => {} } = {}) {
    this.el = el;
    this.url = el.dataset.liveUrl;
    this.version = Number(el.dataset.version);
    this.interval = interval;
    this.canApply = canApply;
    this.onApply = onApply;
    this.queue = [];        // changes waiting to be saved, sent one at a time in order
    this.sending = false;
    this.latest = null;     // newest server HTML not shown yet
    this.retryDelay = 1000;
    this.stopped = false;
    this.status = document.querySelector("[data-sync-status]");
  }

  start() {
    this.timer = setTimeout(() => this.poll(), this.interval);
    // Poll right away when the phone wakes up or the tab comes back; pause while hidden.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.poll();
    });
    window.addEventListener("online", () => this.flush());
    // Warn before leaving the page with changes that haven't reached the server.
    window.addEventListener("beforeunload", (event) => {
      if (this.busy) event.preventDefault();
    });
  }

  // True while anything local hasn't reached the server yet.
  get busy() {
    return this.sending || this.queue.length > 0;
  }

  async poll() {
    clearTimeout(this.timer);
    if (this.stopped) return;
    if (!document.hidden && !this.busy) {
      try {
        const res = await fetch(`${this.url}?since=${this.version}`, {
          headers: { "X-Requested-With": "fetch" }, cache: "no-store",
        });
        if (res.redirected) return this.signedOut();
        if (res.status === 200) this.offer(await res.json());
      } catch {
        // Offline for a moment: just try again next time.
      }
    }
    this.timer = setTimeout(() => this.poll(), this.interval);
  }

  // Show new server HTML now, or as soon as the user isn't in the middle of something.
  offer(data) {
    if (!data || typeof data.html !== "string") return;
    if (this.latest && data.version < this.latest.version) return;
    this.latest = data;
    this.applyLatest();
  }

  applyLatest() {
    if (!this.latest || this.busy || !this.canApply()) return;
    const data = this.latest;
    this.latest = null;

    // Keep collapsed sections the way the user left them.
    const open = new Set(
      Array.from(this.el.querySelectorAll("details[data-keep-open][open]"), (d) => d.dataset.keepOpen),
    );
    this.el.innerHTML = data.html;
    this.el.querySelectorAll("details[data-keep-open]").forEach((d) => {
      if (open.has(d.dataset.keepOpen)) d.open = true;
    });
    this.version = data.version;
    this.el.dataset.version = String(data.version);
    this.onApply();
  }

  // Queue a form submission. `itemId` lets later queued changes to the same item
  // pick up its new revision number (so our own taps never look "stale").
  send(form, { itemId = null } = {}) {
    const body = new URLSearchParams(new FormData(form));
    this.queue.push({ url: form.action, body, itemId });
    this.flush();
  }

  async flush() {
    if (this.sending || this.queue.length === 0 || this.stopped) return;
    this.sending = true;
    const action = this.queue[0];
    let res;
    try {
      res = await fetch(action.url, {
        method: "POST", body: action.body, headers: { "X-Requested-With": "fetch" },
      });
    } catch {
      return this.retryLater("Not saved yet: no connection. It will be sent as soon as you're back online.");
    }

    if (res.redirected) return this.signedOut();
    if (res.status === 403) {
      this.sending = false;
      return this.say("This page expired. Refresh to keep going.", true);
    }
    if (res.status >= 500) return this.retryLater("Couldn't save that yet. Trying again…");

    let data = null;
    try { data = await res.json(); } catch { /* fall through with no data */ }
    this.queue.shift();
    this.sending = false;
    this.retryDelay = 1000;

    if (data && data.rev && action.itemId) {
      // Our own next change to this item should build on the revision we just made.
      this.queue.forEach((a) => {
        if (a.itemId === action.itemId && a.body.has("rev")) a.body.set("rev", String(data.rev));
      });
    }
    if (res.status === 409 || res.status === 404) {
      this.say("Someone just changed the list on another phone, so here's the latest.");
    } else if (data && data.error) {
      this.say(data.error);
    } else {
      this.say("");
    }
    if (data) this.offer(data);
    if (this.queue.length) this.flush();
    else this.applyLatest();
  }

  // Keep the failed change at the front of the queue and try again with backoff.
  retryLater(message) {
    this.sending = false;
    this.say(message, true);
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this.flush(), this.retryDelay);
    this.retryDelay = Math.min(this.retryDelay * 2, 15000);
  }

  signedOut() {
    this.stopped = true;
    this.sending = false;
    this.say("You've been signed out. Refresh the page to sign in again.", true);
  }

  // Small status line under the header; with `retry`, offers a Try now button.
  say(message, retry = false) {
    if (!this.status) return;
    this.status.hidden = !message;
    this.status.textContent = message;
    if (retry && this.queue.length) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "link-button link-button--accent";
      button.textContent = "Try now";
      button.addEventListener("click", () => { this.retryDelay = 1000; this.flush(); });
      this.status.append(" ", button);
    }
  }
}

window.LiveRegion = LiveRegion;
