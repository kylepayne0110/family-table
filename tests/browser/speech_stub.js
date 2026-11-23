// SIMULATED speech for browser tests (headless browsers have no microphone or voices).
// Replaces SpeechRecognition and speechSynthesis with scripted versions the test controls:
//   __speech.say("next")              the "cook" speaks; delivered to the listening recognizer
//   __speech.interim("how much")      a partial result (not final yet)
//   __speech.spoken                   everything the app spoke, with {text, done, cancelled}
//   __speech.autoFinish = false       keep the current utterance "speaking" until finishSpeaking()
//   __speech.echo = true              the phone's own speech is picked up by the microphone
//   __speech.denyMic = true           the microphone permission is refused
// This checks Family Table's conversation handling, not any real speech engine.
(() => {
  const state = {
    spoken: [], recognizers: [], autoFinish: true, finishMs: 40, echo: false, denyMic: false, starts: 0,
    current: null,
  };

  class FakeRecognition {
    constructor() {
      this.continuous = false; this.interimResults = false; this.lang = "en-US"; this.maxAlternatives = 1;
      this.active = false; this.results = [];
    }
    start() {
      if (this.active) throw new Error("InvalidStateError");
      state.starts += 1;
      if (state.denyMic) {
        setTimeout(() => { this.onerror && this.onerror({ error: "not-allowed" }); this.onend && this.onend(); }, 5);
        return;
      }
      this.active = true;
      this.results = [];
      state.recognizers.push(this);
      setTimeout(() => this.onstart && this.onstart(), 1);
    }
    _end() {
      if (!this.active) return;
      this.active = false;
      state.recognizers = state.recognizers.filter((r) => r !== this);
      setTimeout(() => this.onend && this.onend(), 1);
    }
    stop() { this._end(); }
    abort() { this._end(); }
    _deliver(text, isFinal) {
      if (!this.active) return false;
      const result = [{ transcript: text, confidence: 0.9 }];
      result.isFinal = isFinal;
      if (this.results.length && !this.results[this.results.length - 1].isFinal) this.results.pop();
      this.results.push(result);
      const ev = { resultIndex: this.results.length - 1, results: this.results.slice() };
      this.onresult && this.onresult(ev);
      return true;
    }
  }

  const deliver = (text, isFinal) => {
    const live = state.recognizers.filter((r) => r.active);
    live.forEach((r) => r._deliver(text, isFinal));
    return live.length > 0;
  };

  class FakeUtterance {
    constructor(text) { this.text = text; this.voice = null; this.lang = "en-US"; this.rate = 1; }
  }

  const synth = {
    speaking: false,
    queue: [],
    getVoices: () => [{ name: "Test Voice", lang: "en-US", localService: true }],
    speak(u) {
      const entry = { text: u.text, done: false, cancelled: false, u };
      state.spoken.push(entry);
      this.queue.push(entry);
      if (this.queue.length === 1) this._play();
    },
    _play() {
      const entry = this.queue[0];
      if (!entry) { this.speaking = false; return; }
      this.speaking = true;
      state.current = entry;
      if (state.echo) setTimeout(() => deliver(entry.text, true), 5);
      if (state.autoFinish) entry.timer = setTimeout(() => this._finish(entry), state.finishMs);
    },
    _finish(entry) {
      if (entry.done || entry.cancelled) return;
      entry.done = true;
      this.queue.shift();
      entry.u.onend && entry.u.onend({});
      this._play();
    },
    cancel() {
      const pending = this.queue.splice(0);
      pending.forEach((entry) => {
        clearTimeout(entry.timer);
        if (!entry.done) { entry.cancelled = true; entry.u.onerror && entry.u.onerror({ error: "interrupted" }); }
      });
      this.speaking = false;
    },
  };

  window.SpeechRecognition = FakeRecognition;
  window.webkitSpeechRecognition = FakeRecognition;
  window.SpeechSynthesisUtterance = FakeUtterance;
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });

  window.__speech = Object.assign(state, {
    say(text) { deliver(text, false); return deliver(text, true); },
    interim(text) { return deliver(text, false); },
    listening() { return state.recognizers.some((r) => r.active); },
    finishSpeaking() { if (synth.queue[0]) synth._finish(synth.queue[0]); },
    texts() { return state.spoken.map((s) => s.text).join(" | "); },
  });
})();
