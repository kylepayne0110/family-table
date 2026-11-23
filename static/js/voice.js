// Family Table voice: continuous listening and spoken replies with the browser's own speech
// services (no audio is sent to Family Table; only recognized text is).
//
// - Listens continuously by restarting recognition after each utterance (iPhone Safari ends a
//   recognition session after a pause, so a long-lived session isn't reliable there).
// - An utterance is finished after a short silence, so people can finish a thought.
// - While the phone is speaking it keeps listening where the platform allows. Words that
//   match what it's saying are treated as its own echo and ignored; anything else (or
//   "stop") interrupts: speech stops at once and the new words become the next turn.
// - Repeated transcripts are dropped. The microphone is released on mute, end, page hide,
//   and screen lock; resuming after that needs a tap (browsers require it).
"use strict";

(function () {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const STOP_WORDS = new Set(["stop", "wait", "hold on", "hang on", "pause", "shh", "quiet"]);

  const words = (text) => (text || "").toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean);
  const norm = (text) => words(text).join(" ");

  function diag(code) {
    try {
      const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
      const body = new FormData();
      body.append("code", code);
      body.append("csrf_token", token);
      navigator.sendBeacon?.("/diag", body);
    } catch { /* diagnostics are best effort */ }
  }

  class VoiceIO {
    // options: onInterim(text), onUtterance(text), onState(state), onProblem(code), silenceMs, lang
    constructor(options) {
      this.o = Object.assign({ silenceMs: 1100, lang: "en-US", onInterim() {}, onUtterance() {}, onState() {},
        onProblem() {}, onSpeechStart() {} }, options);
      this.state = "off";
      this.muted = false;
      this.speaking = false;
      this.spokenWords = new Set();
      this.spokenUntil = 0;
      this.rec = null;
      this.recActive = false;
      this.heard = "";
      this.lastUtterance = { text: "", at: 0 };
      this.restartFailures = 0;
      this.noSpeech = 0;
      this.bargeIn = true;           // turned off if this browser can't listen while speaking
      this.speechQueue = [];
      this.voice = null;
      this.pickVoice();
      if (window.speechSynthesis) window.speechSynthesis.onvoiceschanged = () => this.pickVoice();
    }

    static support() {
      return { listen: Boolean(Recognition), speak: Boolean(window.speechSynthesis && window.SpeechSynthesisUtterance) };
    }

    pickVoice() {
      if (!window.speechSynthesis) return;
      const voices = window.speechSynthesis.getVoices();
      this.voice = voices.find((v) => v.lang === "en-US" && v.localService) || voices.find((v) => v.lang === "en-US")
        || voices.find((v) => (v.lang || "").startsWith("en")) || null;
    }

    setState(state) {
      if (this.state === state) return;
      this.state = state;
      this.o.onState(state);
    }

    // Must run inside the tap that starts the conversation (iPhone allows speech and the
    // microphone only after a gesture). `greeting` is spoken right away.
    start(greeting) {
      this.ended = false;
      this.paused = false;
      this.muted = false;
      if (greeting) this.speak(greeting);
      this.listen();
      if (!greeting) this.setState("listening");
    }

    // ---- Listening ----
    // The state to show when not speaking or thinking.
    restingState() {
      if (this.blocked) return "blocked";
      if (this.muted) return "muted";
      return Recognition ? "listening" : "typing";
    }

    listen() {
      if (!Recognition) { this.setState(this.speaking ? "speaking" : "typing"); return; }
      if (this.ended || this.paused || this.muted || this.blocked || this.recActive) return;
      if (this.speaking && !this.bargeIn) return;
      const rec = new Recognition();
      rec.lang = this.o.lang;
      rec.continuous = true;
      rec.interimResults = true;
      rec.maxAlternatives = 1;
      this.rec = rec;
      this.heard = "";
      rec.onstart = () => { this.recActive = true; this.restartFailures = 0; if (!this.speaking && !this.thinking) this.setState("listening"); };
      rec.onresult = (e) => { if (this.rec === rec) this.onResult(e); };   // ignore a replaced session's late results
      rec.onerror = (e) => this.onError(e.error);
      rec.onend = () => {
        this.recActive = false;
        if (this.rec !== rec) return;
        this.rec = null;
        if (this.heard && !this.speaking) this.finishUtterance();    // ended mid-thought: keep what we heard
        if (!this.ended && !this.paused && !this.muted && this.state !== "blocked") {
          clearTimeout(this.restartTimer);
          this.restartTimer = setTimeout(() => this.listen(), this.restartFailures ? 600 : 150);
        }
      };
      try {
        this.recActive = true;
        rec.start();
      } catch {
        this.recActive = false;
        this.restartFailures += 1;
        if (this.speaking) { this.bargeIn = false; return; }   // this browser can't listen while speaking
        if (this.restartFailures > 3) { this.setState("needs-tap"); return; }
        this.restartTimer = setTimeout(() => this.listen(), 800);
      }
    }

    onResult(e) {
      this.noSpeech = 0;
      // The whole transcript of this recognition session so far (it may revise itself).
      const text = Array.from(e.results, (r) => r[0].transcript).join(" ").replace(/\s+/g, " ").trim();
      if (!text) return;
      if (this.isEcho(text)) {
        // Our own voice: forget it and start a clean session once we stop talking.
        this.heard = "";
        return;
      }
      if (this.speaking) {
        const said = norm(text);
        // Two or more words, "stop", or any single word the phone isn't saying itself ("next",
        // "repeat") is the cook talking over it.
        if (STOP_WORDS.has(said) || words(text).length >= 2 || !this.spokenWords.has(said)) {
          this.stopSpeaking();                // the cook interrupted: stop at once
          this.o.onSpeechStart();
          if (STOP_WORDS.has(said)) { this.heard = ""; this.restartRecognition(); this.o.onUtterance("stop"); return; }
        } else {
          return;
        }
      }
      this.heard = text;
      this.o.onInterim(text);
      this.setState("hearing");
      clearTimeout(this.silenceTimer);
      const anyFinal = Array.from(e.results).some((r) => r.isFinal);
      this.silenceTimer = setTimeout(() => this.finishUtterance(), anyFinal ? Math.min(700, this.o.silenceMs) : this.o.silenceMs);
    }

    // The phone hearing itself: while it speaks, anything mostly made of its own words. Just after it
    // stops (recognition can lag), only longer runs of its own words; a quick short answer like "yes"
    // or "to taste" is the cook, even if the question used those words.
    isEcho(text) {
      const tail = !this.speaking && Date.now() <= this.spokenUntil;
      if (!this.speaking && !tail) return false;
      const heard = words(text);
      if (!heard.length) return true;
      if (tail && heard.length < 3) return false;
      const overlap = heard.filter((w) => this.spokenWords.has(w)).length / heard.length;
      return overlap >= 0.7 && !(heard.length === 1 && STOP_WORDS.has(heard[0]) && !this.spokenWords.has(heard[0]));
    }

    finishUtterance() {
      clearTimeout(this.silenceTimer);
      const text = this.heard.trim();
      this.heard = "";
      if (!text) return;
      this.restartRecognition();   // the next utterance starts from an empty transcript
      const now = Date.now();
      // The same words again within a moment is the engine repeating itself, not the cook.
      if (norm(text) === norm(this.lastUtterance.text) && now - this.lastUtterance.at < 700) return;
      this.lastUtterance = { text, at: now };
      this.o.onUtterance(text);
    }

    restartRecognition() {
      const rec = this.rec;
      if (!rec) return;
      this.rec = null;
      this.recActive = false;
      try { rec.abort(); } catch { /* already stopped */ }
      clearTimeout(this.restartTimer);
      this.restartTimer = setTimeout(() => this.listen(), 60);
    }

    onError(code) {
      if (code === "not-allowed" || code === "service-not-allowed" || code === "audio-capture") {
        this.blocked = true;
        this.setState("blocked");
        this.o.onProblem(code === "audio-capture" ? "no-mic" : "denied");
        diag(code === "audio-capture" ? "mic_unavailable" : "mic_denied");
        this.stopListening();
      } else if (code === "no-speech") {
        this.noSpeech += 1;
        if (this.noSpeech >= 8) { this.setState("idle"); this.stopListening(); this.o.onProblem("silence"); }
      } else if (code === "network") {
        this.restartFailures += 1;
        this.o.onProblem("network");
        if (this.restartFailures > 3) { this.setState("needs-tap"); this.stopListening(); diag("speech_network"); }
      } else if (code === "aborted") {
        if (this.speaking) this.bargeIn = false;
      }
    }

    stopListening() {
      clearTimeout(this.restartTimer);
      clearTimeout(this.silenceTimer);
      const rec = this.rec;
      this.rec = null;
      this.recActive = false;
      if (rec) { try { rec.abort(); } catch { /* ignore */ } }
    }

    // ---- Speaking ----
    speak(text) {
      return new Promise((resolve) => {
        if (!text) { resolve(true); return; }
        const synth = window.speechSynthesis;
        if (!synth || !window.SpeechSynthesisUtterance) { resolve(true); return; }
        synth.cancel();
        this.speechQueue.forEach((item) => item.resolve(false));
        this.speechQueue = [];
        this.speaking = true;
        words(text).forEach((w) => this.spokenWords.add(w));
        this.setState("speaking");
        if (!this.bargeIn) this.stopListening();
        // Short pieces: long utterances are cut off on some phones.
        const chunks = text.match(/[^.!?;]+[.!?;]*\s*/g) || [text];
        const pieces = [];
        let current = "";
        chunks.forEach((c) => { if ((current + c).length > 180 && current) { pieces.push(current); current = c; } else current += c; });
        if (current.trim()) pieces.push(current);
        const job = { resolve, cancelled: false, left: pieces.length };
        this.speechQueue.push(job);
        pieces.forEach((piece) => {
          const u = new SpeechSynthesisUtterance(piece.trim());
          if (this.voice) u.voice = this.voice;
          u.lang = this.o.lang;
          u.rate = 1;
          const done = () => {
            job.left -= 1;
            clearTimeout(job.fallback);
            if (job.left <= 0 && !job.cancelled) this.speechDone(job, true);
            else job.fallback = setTimeout(() => { if (!job.cancelled && job.left > 0) this.speechDone(job, true); },
              Math.max(4000, piece.length * 120));
          };
          u.onend = done;
          u.onerror = (ev) => { if (ev.error === "interrupted" || ev.error === "canceled") return; done(); };
          synth.speak(u);
        });
        // Some browsers never fire "end": don't stay "speaking" forever.
        job.fallback = setTimeout(() => { if (!job.cancelled && job.left > 0) this.speechDone(job, true); },
          Math.max(5000, text.length * 110));
      });
    }

    speechDone(job, completed) {
      if (job.finished) return;
      job.finished = true;
      clearTimeout(job.fallback);
      this.speechQueue = this.speechQueue.filter((j) => j !== job);
      if (!this.speechQueue.length) {
        this.speaking = false;
        this.spokenUntil = Date.now() + 1200;   // recognition can deliver the phone's last words a moment late
        setTimeout(() => { if (!this.speaking) this.spokenWords.clear(); }, 1500);
        if (!this.ended && !this.paused) {
          this.setState(this.restingState());
          this.listen();
        }
      }
      job.resolve(completed);
    }

    stopSpeaking() {
      const synth = window.speechSynthesis;
      const jobs = this.speechQueue.slice();
      jobs.forEach((job) => { job.cancelled = true; });
      if (synth) synth.cancel();
      jobs.forEach((job) => this.speechDone(job, false));
    }

    // ---- Controls ----
    setThinking(on) {
      this.thinking = on;
      if (on) this.setState("thinking");
      else if (!this.speaking) this.setState(this.restingState());
    }

    mute(on) {
      this.muted = on;
      if (on) { this.stopListening(); this.setState("muted"); }
      else { this.setState("listening"); this.listen(); }
    }

    pause() {
      if (this.ended) return;
      this.paused = true;
      this.stopSpeaking();
      this.stopListening();
      this.setState("paused");
    }

    resume() {     // from a tap
      this.paused = false;
      this.blocked = false;          // a tap tries the microphone again (permission may have been granted)
      this.restartFailures = 0;
      this.noSpeech = 0;
      this.setState(this.speaking ? "speaking" : "listening");
      this.listen();
    }

    end() {
      this.ended = true;
      this.stopSpeaking();
      this.stopListening();
      this.setState("off");
    }
  }

  window.FamilyVoice = { VoiceIO, diag, norm };
})();
