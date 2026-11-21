// "Scan a packaged meal": read a UPC with the camera (or from a photo), look it up on the
// server, and fill in the review form. Everything also works by typing: the digits form and
// the review form are plain HTML forms.
// The planned packaged-meal page uses the photo viewer and "photo chosen" parts.
"use strict";

const FORMATS = ["upc_a", "upc_e", "ean_13", "ean_8"];

// Show which photo was picked (the file inputs themselves are hidden behind buttons).
function setupPhotoChosen() {
  document.querySelectorAll("form").forEach((form) => {
    const note = form.querySelector("[data-photo-chosen]");
    const inputs = form.querySelectorAll("[data-photo-input]");
    if (!note || !inputs.length) return;
    inputs.forEach((input) => input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      // Only one photo is sent: clear the other button's choice.
      inputs.forEach((other) => { if (other !== input) other.value = ""; });
      note.hidden = !file;
      note.textContent = file ? `Photo chosen: ${file.name}` : "";
    }));
  });
}

// Tap the label photo to see it full screen (pinch to zoom works on phones).
function setupViewer() {
  const viewer = document.querySelector("[data-photo-viewer]");
  if (!viewer || typeof viewer.showModal !== "function") return;   // the link opens the photo instead
  const img = viewer.querySelector("[data-viewer-img]");
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-enlarge]");
    if (!link) return;
    event.preventDefault();
    img.src = link.href;
    viewer.showModal();
  });
  viewer.addEventListener("click", (event) => {
    if (event.target.closest("[data-viewer-close]") || event.target === viewer) viewer.close();
  });
}

function setupScanner() {
  const box = document.querySelector("[data-scanner]");
  const form = document.querySelector("[data-review-form]");
  if (!box || !form) return;

  const status = box.querySelector("[data-scan-status]");
  const result = box.querySelector("[data-lookup-result]");
  const digits = box.querySelector("[data-scan-digits]");
  const lookupForm = box.querySelector("[data-lookup-form]");
  const cameraBox = box.querySelector("[data-scan-camera-box]");
  const cameraButton = box.querySelector("[data-scan-camera]");
  const videoBox = box.querySelector("[data-scan-video-box]");
  const video = box.querySelector("[data-scan-video]");
  const stopButton = box.querySelector("[data-scan-stop]");
  const photoBox = box.querySelector("[data-scan-photo-box]");
  const photoInput = box.querySelector("[data-scan-photo]");
  const field = (name) => form.querySelector(`[data-field="${name}"]`);

  const say = (text) => { status.hidden = !text; status.textContent = text; };

  const nativeDetector = async () => {
    if (!("BarcodeDetector" in window)) return null;
    try {
      const supported = await window.BarcodeDetector.getSupportedFormats();
      const formats = FORMATS.filter((f) => supported.includes(f));
      return formats.length ? new window.BarcodeDetector({ formats }) : null;
    } catch { return null; }
  };
  const zxingReader = () => {
    if (!window.ZXing) return null;
    const hints = new Map();
    const F = window.ZXing.BarcodeFormat;
    hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, [F.UPC_A, F.UPC_E, F.EAN_13, F.EAN_8]);
    hints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
    return new window.ZXing.BrowserMultiFormatReader(hints);
  };

  // ---- Showing a lookup result (all text set with textContent: product data is never HTML) ----
  function line(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = text;
    return el;
  }

  function showResult(data) {
    result.replaceChildren();
    result.hidden = false;
    if (data.found) {
      result.append(line("p", "scan-result__title", "Found on Open Food Facts"));
      const parts = [data.name || "(no name listed)", data.brand, data.package_size || "package size not listed"].filter(Boolean);
      const product = line("p", "scan-result__product", "");
      const strong = line("strong", "", parts[0]);
      product.append(strong, document.createTextNode(parts.slice(1).map((p) => ` · ${p}`).join("")));
      result.append(product);
      result.append(line("p", "scan-result__note", data.has_directions
        ? "Check it matches the box in your hand. Cooking directions were found and copied word for word: check them against the package."
        : "Check it matches the box in your hand. Open Food Facts has no cooking directions for this product: photograph the cooking panel or type them below (or add them later)."));
    } else if (data.error) {
      result.append(line("p", "scan-result__title", data.error));
    } else {
      result.append(line("p", "scan-result__title", `No product found for ${data.barcode}.`));
      result.append(line("p", "scan-result__note", "Type the meal's name below; you can still add it."));
    }
  }

  function fill(data) {
    field("barcode").value = data.barcode || "";
    if (data.found) {
      for (const key of ["name", "brand", "package_size"]) if (data[key]) field(key).value = data[key];
      for (const key of ["oven", "microwave", "other"]) {
        const note = form.querySelector(`[data-source-note="${key}"]`);
        if (data[key]) field(key).value = data[key];
        if (note) note.hidden = !data[key];
      }
    }
  }

  async function lookup(code) {
    code = (code || "").replace(/[\s-]/g, "");
    if (!code) { digits.focus(); return; }
    digits.value = code;
    say("Looking it up…");
    try {
      const res = await fetch(`/packaged/lookup?barcode=${encodeURIComponent(code)}`, {
        headers: { "X-Requested-With": "fetch" }, cache: "no-store",
      });
      if (res.redirected) { window.location.href = "/login"; return; }
      const data = await res.json();
      if (res.status === 422) { say(data.problem); digits.focus(); return; }
      say("");
      showResult(data);
      fill(data);
      field("name").scrollIntoView({ behavior: "smooth", block: "center" });
    } catch {
      say("Couldn't reach the product lookup. Type the meal's name below; you can still add it.");
      fill({ barcode: code });
    }
  }

  lookupForm.addEventListener("submit", (event) => {
    event.preventDefault();
    lookup(digits.value);
  });
  box.querySelector("[data-scan-manual]")?.addEventListener("click", () => {
    setTimeout(() => field("name").focus(), 50);
  });
  // Edited directions are no longer "copied from Open Food Facts".
  for (const key of ["oven", "microwave", "other"]) {
    field(key).addEventListener("input", () => {
      const note = form.querySelector(`[data-source-note="${key}"]`);
      if (note) note.hidden = true;
    });
  }

  // ---- Reading a barcode from a photo (works without live camera access) ----
  photoBox.hidden = false;
  photoInput.addEventListener("change", async () => {
    const file = photoInput.files && photoInput.files[0];
    if (!file) return;
    say("Reading the barcode…");
    try {
      let code = null;
      const detector = await nativeDetector();
      if (detector) {
        const found = await detector.detect(await createImageBitmap(file));
        code = found[0] && found[0].rawValue;
      }
      if (!code) {
        const reader = zxingReader();
        if (reader) {
          const url = URL.createObjectURL(file);
          try { code = (await reader.decodeFromImageUrl(url)).getText(); } finally { URL.revokeObjectURL(url); }
        }
      }
      if (!code) throw new Error("none");
      lookup(code);
    } catch {
      say("Couldn't read a barcode in that photo. Try again closer and in focus, or type the digits.");
    } finally {
      photoInput.value = "";
    }
  });

  // ---- Live camera scanning ----
  // One loop for every browser: open the camera, then read a frame every 200 ms with the
  // built-in BarcodeDetector (Chrome on Android) or ZXing (iPhone Safari, desktop browsers).
  const cameraPossible = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  if (!cameraPossible) {
    say("Live scanning needs the secure (https) address. Take a photo of the barcode or type the digits instead.");
    return;
  }
  cameraBox.hidden = false;
  let stream = null;
  let timer = null;

  async function frameDecoder() {
    const detector = await nativeDetector();
    if (detector) {
      return async () => { const codes = await detector.detect(video); return codes.length ? codes[0].rawValue : null; };
    }
    if (!window.ZXing) return null;
    const Z = window.ZXing;
    const reader = new Z.MultiFormatReader();
    const hints = new Map();
    hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E, Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8]);
    hints.set(Z.DecodeHintType.TRY_HARDER, true);
    reader.setHints(hints);
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    return async () => {
      const w = video.videoWidth;
      const h = video.videoHeight;
      if (!w || !h) return null;
      const scale = Math.min(1, 1280 / Math.max(w, h));      // big phone frames are slow to scan
      canvas.width = Math.round(w * scale);
      canvas.height = Math.round(h * scale);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      try {
        const bitmap = new Z.BinaryBitmap(new Z.HybridBinarizer(new Z.HTMLCanvasElementLuminanceSource(canvas)));
        return reader.decodeWithState(bitmap).getText();
      } catch { return null; }                              // no barcode in this frame yet
    };
  }

  function stopCamera() {
    clearTimeout(timer);
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    video.srcObject = null;
    videoBox.hidden = true;
    cameraButton.hidden = false;
  }

  function cameraFailed(error) {
    stopCamera();
    const denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
    say(denied
      ? "Camera access is off for this site. Take a photo of the barcode, type the digits, or enter the meal's name. (You can turn the camera back on in your browser's site settings.)"
      : error && error.name === "NotFoundError"
        ? "No camera was found. Type the digits under the barcode, or enter the meal's name."
        : "The camera couldn't start. Type the digits under the barcode, or enter the meal's name.");
    digits.focus();
  }

  function found(code) {
    stopCamera();
    if (navigator.vibrate) navigator.vibrate(60);
    lookup(code);
  }

  cameraButton.addEventListener("click", async () => {
    say("Starting the camera\u2026");
    cameraButton.hidden = true;
    videoBox.hidden = false;
    try {
      const decode = await frameDecoder();
      if (!decode) throw Object.assign(new Error("no decoder"), { name: "NoDecoder" });
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
      video.srcObject = stream;
      await video.play();
      say("Point the camera at the barcode.");
      const tick = async () => {
        if (!stream) return;
        let code = null;
        try { if (video.readyState >= 2) code = await decode(); } catch { /* try the next frame */ }
        if (code) { found(code); return; }
        timer = setTimeout(tick, 200);
      };
      tick();
    } catch (error) {
      cameraFailed(error);
    }
  });
  stopButton.addEventListener("click", () => { stopCamera(); say(""); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopCamera(); });
}

document.addEventListener("DOMContentLoaded", () => {
  setupPhotoChosen();
  setupViewer();
  setupScanner();
});
