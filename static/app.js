const gate = document.querySelector("#gate");
const shell = document.querySelector("#shell");
const gateForm = document.querySelector("#gate-form");
const gateTitle = document.querySelector("#gate-title");
const gateLead = document.querySelector("#gate-lead");
const gateError = document.querySelector("#gate-error");
const confirmWrap = document.querySelector("#confirm-wrap");
const views = {
  home: document.querySelector("#view-home"),
  log: document.querySelector("#view-log"),
  settings: document.querySelector("#view-settings"),
};

let setupMode = false;
let settings = null;
let activity = [];
let filter = "work";
let hookToken = "";

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "請求失敗");
  return data;
}

function showApp(on) {
  gate.classList.toggle("hidden", on);
  shell.classList.toggle("hidden", !on);
}

function setView(name) {
  Object.entries(views).forEach(([key, node]) => node.classList.toggle("hidden", key !== name));
  document.querySelectorAll(".nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  if (name === "log") loadLog();
  if (name === "settings") fillSettings();
}

document.querySelectorAll(".nav button").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

gateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  gateError.textContent = "";
  const body = {
    username: document.querySelector("#username").value.trim(),
    password: document.querySelector("#password").value,
    confirm: document.querySelector("#confirm").value,
  };
  try {
    await api(setupMode ? "/api/setup" : "/api/login", { method: "POST", body });
    await bootApp();
  } catch (error) {
    gateError.textContent = error.message;
  }
});

document.querySelector("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: {} });
  showApp(false);
  await bootGate();
});

function statusLabel(status) {
  if (status === "ok") return "成功";
  if (status === "error") return "失敗";
  if (status === "recv") return "收到";
  return "略過";
}

function row(item) {
  const node = document.createElement("div");
  node.className = "row";
  const when = document.createElement("div");
  when.textContent = item.ts || "";
  const badge = document.createElement("div");
  badge.className = "pill " + (item.status === "ok" ? "ok" : item.status === "error" ? "bad" : "wait");
  badge.textContent = statusLabel(item.status);
  const paths = document.createElement("div");
  paths.className = "paths";
  const title = document.createElement("div");
  title.textContent = item.torrent || item.detail || "";
  const oldPath = document.createElement("div");
  oldPath.textContent = item.old_path || "";
  const neu = document.createElement("div");
  neu.textContent = item.new_path ? "→ " + item.new_path : "";
  const detail = document.createElement("div");
  detail.className = "muted";
  detail.textContent = item.torrent ? item.detail : "";
  paths.append(title, oldPath, neu, detail);
  if (item.retryable && item.status === "error") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn retry";
    retry.textContent = "重試";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      retry.textContent = "已排程";
      try {
        await api("/api/activity/retry", { method: "POST", body: { id: item.id } });
        await loadLog();
        await refreshHome();
      } catch (error) {
        retry.disabled = false;
        retry.textContent = "重試";
        detail.textContent = error.message;
      }
    });
    paths.append(retry);
  }
  node.append(when, badge, paths);
  return node;
}

function renderRows(target, items) {
  target.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "未有紀錄。CloudDrive 一通知改名，就會出現喺度。";
    target.append(empty);
    return;
  }
  items.forEach((item) => target.append(row(item)));
}

async function refreshHome() {
  const data = await api("/api/overview");
  document.querySelector("#count-ok").textContent = data.counts.ok || 0;
  document.querySelector("#count-bad").textContent = (data.counts.error || 0) + (data.counts.skip || 0);
  document.querySelector("#count-pending").textContent = data.pending || 0;
  document.querySelector("#last-report").textContent = data.last_report || "";
  const checks = document.querySelector("#checks");
  checks.replaceChildren();
  const lines = [
    ["qBittorrent", data.qb],
    ["媒體目錄", data.media],
    ["自動對齊", { ok: data.enabled && !data.dry_run, detail: data.enabled ? (data.dry_run ? "只記錄" : "運作中") : "未開啟" }],
  ];
  lines.forEach(([label, item]) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = label + " · " + (item.detail || "");
    const pill = document.createElement("span");
    const known = item.ok === true || item.ok === false;
    pill.className = "pill " + (item.ok === true ? "ok" : item.ok === false ? "bad" : "wait");
    pill.textContent = item.ok === true ? "正常" : known ? "未就緒" : "檢查中";
    li.append(name, pill);
    checks.append(li);
  });
  const banner = document.querySelector("#banner");
  if (!data.enabled || data.dry_run) {
    banner.classList.remove("hidden");
    banner.textContent = data.dry_run
      ? "而家只記錄，未會改 qBittorrent。路徑同連線確認後，按「開始自動改名」。"
      : "自動對齊未開啟。設定好 qBittorrent、CloudDrive 同路徑後再開。";
  } else {
    banner.classList.add("hidden");
  }
  renderRows(
    document.querySelector("#recent"),
    (data.recent || []).filter((item) => item.status !== "recv").slice(0, 8),
  );
}

async function loadLog() {
  const data = await api("/api/activity?limit=300");
  activity = data.items || [];
  paintLog();
}

function paintLog() {
  const query = document.querySelector("#search").value.trim().toLowerCase();
  const items = activity.filter((item) => {
    if (filter === "work" && (item.status === "recv")) return false;
    if (filter === "ok" && item.status !== "ok") return false;
    if (filter === "error" && item.status !== "error") return false;
    if (!query) return true;
    return [item.torrent, item.old_path, item.new_path, item.detail].join(" ").toLowerCase().includes(query);
  });
  renderRows(document.querySelector("#log-list"), items);
}

document.querySelector("#search").addEventListener("input", paintLog);
document.querySelectorAll("#filters button").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll("#filters button").forEach((item) => item.classList.toggle("active", item === button));
    paintLog();
  });
});

function mapRow(item = { cd_prefix: "", local_prefix: "/media", qbit_prefix: "/downloads" }) {
  const wrap = document.createElement("div");
  wrap.className = "map";
  ["cd_prefix", "local_prefix", "qbit_prefix"].forEach((key) => {
    const input = document.createElement("input");
    input.dataset.key = key;
    input.value = item[key] || "";
    input.placeholder = key === "cd_prefix" ? "CloudDrive 前綴" : key === "local_prefix" ? "容器內路徑" : "qB 儲存前綴";
    input.setAttribute("aria-label", input.placeholder);
    wrap.append(input);
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn";
  remove.textContent = "移除";
  remove.addEventListener("click", () => wrap.remove());
  wrap.append(remove);
  return wrap;
}

function readMaps() {
  return [...document.querySelectorAll("#maps .map")].map((node) => {
    const values = {};
    node.querySelectorAll("input").forEach((input) => {
      values[input.dataset.key] = input.value.trim();
    });
    return values;
  });
}

function formSettings() {
  const qb = Object.fromEntries(new FormData(document.querySelector("#form-qb")));
  const cd = Object.fromEntries(new FormData(document.querySelector("#form-cd")));
  const auto = Object.fromEntries(new FormData(document.querySelector("#form-auto")));
  const notify = Object.fromEntries(new FormData(document.querySelector("#form-notify")));
  return {
    enabled: Boolean(auto.enabled),
    dry_run: Boolean(auto.dry_run),
    rename_torrent_title: Boolean(auto.rename_torrent_title),
    debounce_sec: Number(auto.debounce_sec || 8),
    webhook_token: auto.webhook_token || "",
    watch_folders: notify.watch_folders || "",
    telegram_bot_token: notify.telegram_bot_token || "",
    telegram_chat_id: notify.telegram_chat_id || "",
    tmdb_api_key: notify.tmdb_api_key || "",
    qb: {
      host: qb.host || "",
      port: Number(qb.port || 8080),
      https: Boolean(qb.https),
      username: qb.username || "",
      password: qb.password || "",
    },
    clouddrive: {
      host: cd.host || "",
      port: Number(cd.port || 19798),
      https: Boolean(cd.https),
    },
    maps: readMaps(),
  };
}

function fillSettings() {
  if (!settings) return;
  const qbForm = document.querySelector("#form-qb");
  qbForm.host.value = settings.qb.host || "";
  qbForm.port.value = settings.qb.port || 8080;
  qbForm.https.checked = Boolean(settings.qb.https);
  qbForm.username.value = settings.qb.username || "";
  qbForm.password.value = "";
  const cdForm = document.querySelector("#form-cd");
  cdForm.host.value = settings.clouddrive.host || "";
  cdForm.port.value = settings.clouddrive.port || 19798;
  cdForm.https.checked = Boolean(settings.clouddrive.https);
  const auto = document.querySelector("#form-auto");
  auto.debounce_sec.value = settings.debounce_sec || 8;
  auto.enabled.checked = Boolean(settings.enabled);
  auto.dry_run.checked = Boolean(settings.dry_run);
  auto.rename_torrent_title.checked = Boolean(settings.rename_torrent_title);
  auto.webhook_token.value = settings.webhook_token || "";
  hookToken = settings.webhook_token || "";
  const notify = document.querySelector("#form-notify");
  notify.watch_folders.value = settings.watch_folders || "";
  notify.telegram_bot_token.value = "";
  notify.telegram_bot_token.placeholder = settings.telegram_token_set ? "已儲存，留空不改" : "留空不改";
  notify.telegram_chat_id.value = settings.telegram_chat_id || "";
  notify.tmdb_api_key.value = "";
  notify.tmdb_api_key.placeholder = settings.tmdb_api_key_set ? "已儲存，留空不改" : "留空不改";
  const maps = document.querySelector("#maps");
  maps.replaceChildren();
  (settings.maps || []).forEach((item) => maps.append(mapRow(item)));
  const url = location.origin + "/file_notify?token=" + encodeURIComponent(hookToken);
  document.querySelector("#hook-url").textContent = url;
  const recv = activity.find((item) => item.status === "recv");
  document.querySelector("#raw-hint").textContent = recv
    ? "最近 webhook source：" + recv.old_path
    : "未收到 webhook。填好 CloudDrive 之後，改一個資料夾名就會出現喺度。";
}

document.querySelector("#add-map").addEventListener("click", () => {
  document.querySelector("#maps").append(mapRow());
});

document.querySelector("#save").addEventListener("click", async () => {
  const message = document.querySelector("#save-msg");
  message.textContent = "";
  try {
    settings = await api("/api/settings", { method: "POST", body: formSettings() });
    message.style.color = "var(--ok)";
    message.textContent = "已儲存";
    fillSettings();
    refreshHome();
  } catch (error) {
    message.style.color = "var(--danger)";
    message.textContent = error.message;
  }
});

async function runTest(path, outputId) {
  const node = document.querySelector(outputId);
  node.textContent = "測試中…";
  try {
    const result = await api(path, { method: "POST", body: formSettings() });
    node.textContent = result.detail || (result.ok ? "成功" : "失敗");
  } catch (error) {
    node.textContent = error.message;
  }
}

document.querySelector("#test-qb").addEventListener("click", () => runTest("/api/test/qb", "#out-qb"));
document.querySelector("#test-cd").addEventListener("click", () => runTest("/api/test/cd", "#out-cd"));
document.querySelector("#test-media").addEventListener("click", () => runTest("/api/test/media", "#out-media"));

document.querySelector("#arm").addEventListener("click", async () => {
  try {
    settings = await api("/api/arm", { method: "POST", body: {} });
    await refreshHome();
    fillSettings();
  } catch (error) {
    const banner = document.querySelector("#banner");
    banner.classList.remove("hidden");
    banner.textContent = error.message;
  }
});

document.querySelector("#copy-hook").addEventListener("click", async () => {
  const text = document.querySelector("#hook-url").textContent;
  try {
    await navigator.clipboard.writeText(text);
    document.querySelector("#copy-hook").textContent = "已複製";
  } catch {
    document.querySelector("#copy-hook").textContent = "請手動複製";
  }
});

async function bootGate() {
  const session = await api("/api/session");
  setupMode = Boolean(session.setup);
  confirmWrap.classList.toggle("hidden", !setupMode);
  gateTitle.textContent = setupMode ? "建立管理員" : "登入";
  document.querySelector("#gate-submit").textContent = setupMode ? "建立並進入" : "進入";
  gateLead.textContent = setupMode
    ? "這是容器裡的登入，只存喺呢個 Seedbridge，唔係 qBittorrent 帳號。"
    : "Filmix 改名之後，自動幫 qBittorrent 對齊資料夾同檔名。";
  document.querySelector("#username").value = setupMode ? "admin" : "";
  document.querySelector("#password").autocomplete = setupMode ? "new-password" : "current-password";
  if (session.user) await bootApp();
}

async function bootApp() {
  settings = await api("/api/settings");
  hookToken = settings.webhook_token || "";
  const log = await api("/api/activity?limit=50");
  activity = log.items || [];
  showApp(true);
  setView("home");
  fillSettings();
  await refreshHome();
}

bootGate().catch((error) => {
  gateError.textContent = error.message;
});

setInterval(() => {
  if (!shell.classList.contains("hidden") && !views.home.classList.contains("hidden")) {
    refreshHome().catch(() => {});
  }
}, 4000);
