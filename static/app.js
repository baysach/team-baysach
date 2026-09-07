const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const cookieInput = document.getElementById("cookie");
const searchUrlInput = document.getElementById("search-url");
const payloadInput = document.getElementById("payload");
const validationMessage = document.getElementById("validation-message");
const toggleCookieButton = document.getElementById("toggle-cookie");
const validateButton = document.getElementById("validate-json");
const startDryButton = document.getElementById("start-dry");
const startSendButton = document.getElementById("start-send");
const readyNextButton = document.getElementById("ready-next");
const stopButton = document.getElementById("stop");
const clearLogButton = document.getElementById("clear-log");
const eventLog = document.getElementById("event-log");
const emptyState = document.getElementById("empty-state");
const statusPill = document.getElementById("connection-status");
const statusLabel = document.getElementById("connection-label");

const STORAGE_URL = "poe-live-alert.search-url";
const STORAGE_PAYLOAD = "poe-live-alert.search-payload";

let alertAudio = null;

async function enableAlertAudio() {
  try {
    if (!alertAudio) alertAudio = new AudioContext();
    if (alertAudio.state === "suspended") await alertAudio.resume();
  } catch (error) {
    console.warn("Could not enable alert sound:", error);
  }
}

function playAlertSound() {
  if (!alertAudio || alertAudio.state !== "running") return;
  const start = alertAudio.currentTime;
  [880, 1174.66].forEach((frequency, index) => {
    const oscillator = alertAudio.createOscillator();
    const gain = alertAudio.createGain();
    const at = start + index * 0.2;
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0, at);
    gain.gain.linearRampToValueAtTime(0.18, at + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.001, at + 0.18);
    oscillator.connect(gain);
    gain.connect(alertAudio.destination);
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
    };
    oscillator.start(at);
    oscillator.stop(at + 0.19);
  });
}

// User interaction unlocks audio, including after refreshing a running monitor.
document.addEventListener("click", enableAlertAudio);

function restoreSearch() {
  const savedUrl = localStorage.getItem(STORAGE_URL);
  const savedPayload = localStorage.getItem(STORAGE_PAYLOAD);
  if (savedUrl) searchUrlInput.value = savedUrl;
  if (savedPayload) payloadInput.value = savedPayload;
}

function saveSearch() {
  localStorage.setItem(STORAGE_URL, searchUrlInput.value.trim());
  localStorage.setItem(STORAGE_PAYLOAD, payloadInput.value);
}

function parsePayload(showResult = true) {
  try {
    const parsed = JSON.parse(payloadInput.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Payload must be a JSON object.");
    }
    if (!parsed.query || typeof parsed.query !== "object" || Array.isArray(parsed.query)) {
      throw new Error("Payload must contain a query object.");
    }
    if (showResult) {
      payloadInput.value = JSON.stringify(parsed, null, 2);
      validationMessage.textContent = "Valid JSON — ready to use.";
      validationMessage.className = "validation-message valid";
    }
    return parsed;
  } catch (error) {
    if (showResult) {
      validationMessage.textContent = error.message;
      validationMessage.className = "validation-message invalid";
    }
    throw error;
  }
}

function setRunning(state) {
  const running = Boolean(state.running);
  startDryButton.disabled = running;
  startSendButton.disabled = running;
  readyNextButton.disabled = !(running && state.mode === "send" && state.busy);
  stopButton.disabled = !running;
  cookieInput.disabled = running;
  searchUrlInput.disabled = running;
  payloadInput.disabled = running;
  validateButton.disabled = running;
  toggleCookieButton.disabled = running;

  if (running && state.authenticated && state.busy) {
    statusPill.dataset.state = "busy";
    statusLabel.textContent = "Live · Busy";
  } else if (running && state.authenticated) {
    statusPill.dataset.state = "running";
    statusLabel.textContent = state.mode === "send" ? "Live · Sending" : "Live · Dry run";
  } else if (running) {
    statusPill.dataset.state = "connecting";
    statusLabel.textContent = "Connecting";
  } else {
    statusPill.dataset.state = "idle";
    statusLabel.textContent = "Idle";
    cookieInput.value = "";
  }
}

function eventDetails(details) {
  if (!details || typeof details !== "object") return "";
  const labels = {
    seller: "Seller",
    listing_id: "Listing",
    query_id: "Query",
    count: "Count",
    http_status: "HTTP status",
  };
  return Object.entries(details)
    .filter(([key, value]) => !["response_body", "sound"].includes(key) && value !== null && value !== undefined)
    .map(([key, value]) => `${labels[key] || key}: ${String(value)}`)
    .join(" · ");
}

function appendEvent(event) {
  const currentEmptyState = eventLog.querySelector(".empty-state");
  if (currentEmptyState) currentEmptyState.remove();
  const row = document.createElement("div");
  row.className = "event-row";
  row.dataset.level = event.level || "info";

  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = event.time || "--:--:--";

  const marker = document.createElement("span");
  marker.className = "event-marker";
  marker.setAttribute("aria-hidden", "true");

  const copy = document.createElement("div");
  copy.className = "event-copy";
  const message = document.createElement("p");
  message.className = "event-message";
  message.textContent = event.message || "Activity update";
  copy.appendChild(message);

  const detailText = eventDetails(event.details);
  if (detailText) {
    const details = document.createElement("p");
    details.className = "event-details";
    details.textContent = detailText;
    copy.appendChild(details);
  }

  if (typeof event.details?.response_body === "string") {
    const responseBody = document.createElement("pre");
    responseBody.className = "event-details";
    responseBody.style.whiteSpace = "pre-wrap";
    responseBody.style.overflowWrap = "anywhere";
    responseBody.textContent = `Response body:\n${event.details.response_body || "(empty response body)"}`;
    copy.appendChild(responseBody);
  }

  row.append(time, marker, copy);
  eventLog.appendChild(row);
  while (eventLog.children.length > 150) eventLog.firstElementChild.remove();
  eventLog.scrollTop = eventLog.scrollHeight;
}

async function postJson(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    cache: "no-store",
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function startMonitor(sendWhispers) {
  let payload;
  try {
    payload = parsePayload(true);
  } catch (_) {
    payloadInput.focus();
    return;
  }

  if (!cookieInput.value.trim()) {
    validationMessage.textContent = "Cookie Header is required.";
    validationMessage.className = "validation-message invalid";
    cookieInput.focus();
    return;
  }
  if (!searchUrlInput.value.trim()) {
    validationMessage.textContent = "Search URL is required.";
    validationMessage.className = "validation-message invalid";
    searchUrlInput.focus();
    return;
  }
  if (sendWhispers) {
    const confirmed = window.confirm(
      "Start automatic travel to hideout? In-demand items will automatically continue past the site's confirmation. After travel is accepted, travel pauses until you click Ready for Next Alert."
    );
    if (!confirmed) return;
  }

  saveSearch();
  setRunning({ running: true, authenticated: false, mode: sendWhispers ? "send" : "dry-run" });
  try {
    await postJson("/api/start", {
      cookie: cookieInput.value.trim(),
      search_url: searchUrlInput.value.trim(),
      payload,
      send_whispers: sendWhispers,
    });
    cookieInput.value = "";
  } catch (error) {
    appendEvent({ level: "error", message: error.message, time: new Date().toLocaleTimeString([], { hour12: false }) });
    setRunning({ running: false });
  }
}

toggleCookieButton.addEventListener("click", () => {
  const hidden = cookieInput.type === "password";
  cookieInput.type = hidden ? "text" : "password";
  toggleCookieButton.textContent = hidden ? "Hide" : "Show";
});

validateButton.addEventListener("click", () => {
  try { parsePayload(true); } catch (_) { /* validation is shown inline */ }
});

startDryButton.addEventListener("click", () => startMonitor(false));
startSendButton.addEventListener("click", () => startMonitor(true));
readyNextButton.addEventListener("click", async () => {
  readyNextButton.disabled = true;
  try {
    await postJson("/api/ready");
  } catch (error) {
    appendEvent({ level: "error", message: error.message, time: new Date().toLocaleTimeString([], { hour12: false }) });
  }
});
stopButton.addEventListener("click", async () => {
  stopButton.disabled = true;
  try {
    await postJson("/api/stop");
  } catch (error) {
    appendEvent({ level: "error", message: error.message, time: new Date().toLocaleTimeString([], { hour12: false }) });
  }
});

clearLogButton.addEventListener("click", () => {
  eventLog.replaceChildren();
  const replacement = document.createElement("div");
  replacement.className = "empty-state";
  replacement.innerHTML = '<span class="empty-icon" aria-hidden="true">↯</span><p>Activity cleared.</p><span>New events will appear here.</span>';
  eventLog.appendChild(replacement);
});

const eventSource = new EventSource("/api/events");
eventSource.onmessage = (message) => {
  try {
    const event = JSON.parse(message.data);
    if (event.type === "state") setRunning(event);
    if (event.type === "log") {
      appendEvent(event);
      if (event.details?.sound === true && !event.replayed) playAlertSound();
    }
  } catch (_) { /* ignore malformed local events */ }
};

window.addEventListener("beforeunload", () => { cookieInput.value = ""; });

restoreSearch();
fetch("/api/state", { cache: "no-store" })
  .then((response) => response.json())
  .then(setRunning)
  .catch(() => setRunning({ running: false }));
