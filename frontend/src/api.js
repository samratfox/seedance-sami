const API_BASE = (import.meta.env.VITE_API_URL || window.location.origin).replace(/\/$/, "");

export function getInitData() {
  const tg = window.Telegram?.WebApp;
  return tg?.initData || import.meta.env.VITE_DEV_INIT_DATA || "";
}

export function isTelegramReady() {
  return Boolean(window.Telegram?.WebApp?.initData || import.meta.env.VITE_DEV_INIT_DATA);
}

export function absoluteUrl(url) {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return url;
  return `${API_BASE}${url.startsWith("/") ? url : `/${url}`}`;
}

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return { detail: await response.text() };
}

export function errorMessage(value, fallback = "Request failed") {
  if (!value) return fallback;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const lines = value.map((item) => errorMessage(item, "")).filter(Boolean);
    return lines.length ? lines.join("\n") : fallback;
  }
  if (typeof value === "object") {
    if (typeof value.detail === "string") return value.detail;
    if (value.detail) return errorMessage(value.detail, fallback);
    if (typeof value.message === "string") return value.message;
    if (typeof value.msg === "string") {
      const loc = Array.isArray(value.loc) ? `${value.loc.join(".")}: ` : "";
      return `${loc}${value.msg}`;
    }
    if (value.error) return errorMessage(value.error, fallback);
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function throwResponseError(payload, statusText) {
  throw new Error(errorMessage(payload?.detail ?? payload, statusText));
}

async function postForm(endpoint, extra = {}) {
  const form = new FormData();
  form.append("init_data", getInitData());
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null && value !== "") form.append(key, value);
  }

  const response = await fetch(`${API_BASE}${endpoint}`, { method: "POST", body: form });
  const payload = await readResponse(response);
  if (!response.ok) throwResponseError(payload, response.statusText);
  return payload;
}

export async function fetchConfig() {
  const response = await fetch(`${API_BASE}/api/config`);
  const payload = await readResponse(response);
  if (!response.ok) throwResponseError(payload, response.statusText);
  return payload;
}

export async function fetchBalance() {
  return postForm("/api/balance");
}

export async function submitGeneration({
  modelMode,
  prompt,
  duration,
  resolution,
  ratio,
  audio,
  negativePrompt,
  seed,
  imageFiles,
  videoFile,
  audioFile,
}) {
  const form = new FormData();
  form.append("init_data", getInitData());
  form.append("model_mode", modelMode);
  form.append("prompt", prompt);
  form.append("duration", duration);
  form.append("resolution", resolution);
  form.append("ratio", ratio);
  form.append("audio", audio ? "true" : "false");
  if (negativePrompt?.trim()) form.append("negative_prompt", negativePrompt.trim());
  if (seed !== "" && seed !== null && seed !== undefined) form.append("seed", seed);
  for (const file of imageFiles) form.append("image_files", file);
  if (videoFile) form.append("video_file", videoFile);
  if (audioFile) form.append("audio_file", audioFile);

  const response = await fetch(`${API_BASE}/api/generate`, { method: "POST", body: form });
  const payload = await readResponse(response);
  if (!response.ok) throwResponseError(payload, response.statusText);
  return payload;
}

export async function fetchHistory(limit = 8) {
  return postForm("/api/history", { limit });
}

export async function setApiKey(key) {
  return postForm("/api/setkey", { api_key: key });
}

export function connectWebSocket(onMessage) {
  const initData = getInitData();
  if (!initData) return null;

  const protocol = API_BASE.startsWith("https") ? "wss" : "ws";
  const host = API_BASE.replace(/^https?:\/\//, "");
  const websocket = new WebSocket(`${protocol}://${host}/ws?init_data=${encodeURIComponent(initData)}`);
  websocket.onmessage = (event) => onMessage(JSON.parse(event.data));
  websocket.onerror = (event) => console.error("WebSocket error", event);
  return websocket;
}
