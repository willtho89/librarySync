const authState = {
  user: null,
  loaded: false,
  promise: null,
};
const lookupState = {
  id: null,
  timer: null,
  candidates: [],
};
const episodeState = {
  tmdbId: null,
  seasonNumber: null,
};
const DEFAULT_IMPORT_INTERVAL_SECONDS = 86400;

function bindForm(id, handler) {
  const form = document.getElementById(id);
  if (!form) {
    return;
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    handler(new FormData(form), form);
  });
}

function setMessage(id, message, isError = false) {
  const el = document.getElementById(id);
  if (!el) {
    return;
  }
  el.textContent = message;
  el.dataset.state = isError ? "error" : "success";
  el.hidden = !message;
}

function parseIntervalSeconds(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return null;
  }
  if (numeric === 0) {
    return 0;
  }
  return Math.trunc(numeric);
}

function formatImportTimestamp(value) {
  if (!value) {
    return "Last import: Never";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "Last import: Unknown";
  }
  return `Last import: ${date.toLocaleString()}`;
}

function applyImportControls(provider, integration) {
  const form = document.getElementById(`${provider}-import-form`);
  if (!form) {
    return;
  }
  const select = form.querySelector("select[name='import_interval']");
  const lastEl = document.getElementById(`${provider}-import-last`);
  const importNow = document.getElementById(`${provider}-import-now`);
  const hasSecrets = integration && integration.has_secrets;
  if (select) {
    const rawInterval =
      integration && integration.config
        ? integration.config.import_interval_seconds
        : null;
    let intervalValue = parseIntervalSeconds(rawInterval);
    if (intervalValue === null && hasSecrets) {
      intervalValue = DEFAULT_IMPORT_INTERVAL_SECONDS;
    }
    select.value = intervalValue ? String(intervalValue) : "";
    select.disabled = !hasSecrets;
  }
  if (importNow) {
    importNow.disabled = !hasSecrets;
  }
  if (!hasSecrets) {
    if (lastEl) {
      lastEl.textContent = "Connect to enable history import.";
    }
    setMessage(`${provider}-import-message`, "");
    return;
  }
  if (lastEl) {
    const lastRun =
      integration && integration.config
        ? integration.config.import_last_run_at
        : null;
    lastEl.textContent = formatImportTimestamp(lastRun);
  }
}

function isIntegrationConnected(integration) {
  return (
    !!integration &&
    (integration.has_secrets || integration.status === "connected")
  );
}

async function requestJSON(path, options = {}) {
  const headers = Object.assign(
    {},
    options.headers || {},
    options.body ? { "Content-Type": "application/json" } : {}
  );
  const response = await fetch(path, {
    credentials: "include",
    ...options,
    headers,
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    const message =
      (data && (data.detail || data.message)) ||
      `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function loadCurrentUser() {
  if (authState.loaded) {
    return authState.user;
  }
  if (!authState.promise) {
    authState.promise = requestJSON("/api/auth/me")
      .then((user) => {
        authState.user = user;
        authState.loaded = true;
        return user;
      })
      .catch((error) => {
        if (error.status !== 401) {
          console.error("auth check failed", error);
        }
        authState.user = null;
        authState.loaded = true;
        return null;
      });
  }
  return authState.promise;
}

function applyAuthVisibility(user) {
  document.querySelectorAll("[data-auth-only]").forEach((el) => {
    el.hidden = !user;
  });
  document.querySelectorAll("[data-guest-only]").forEach((el) => {
    el.hidden = !!user;
  });
  document.querySelectorAll("[data-user-username]").forEach((el) => {
    el.textContent = user ? user.username : "";
  });
}

async function handleLogin(data) {
  setMessage("login-message", "");
  const payload = Object.fromEntries(data.entries());
  try {
    await requestJSON("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    window.location.href = "/";
  } catch (error) {
    setMessage("login-message", error.message, true);
  }
}

async function handleRegister(data) {
  setMessage("register-message", "");
  const payload = Object.fromEntries(data.entries());
  try {
    await requestJSON("/api/auth/register", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await requestJSON("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    window.location.href = "/";
  } catch (error) {
    setMessage("register-message", error.message, true);
  }
}

async function handleLogout() {
  try {
    await requestJSON("/api/auth/logout", { method: "POST" });
  } catch (error) {
    console.error("logout failed", error);
  }
  window.location.href = "/static/login.html";
}

async function loadIntegrations() {
  const data = await requestJSON("/api/integrations");
  const integrations = data && data.integrations ? data.integrations : [];
  const letterboxdForm = document.getElementById("letterboxd-form");
  const letterboxd = integrations.find((item) => item.provider === "letterboxd");
  if (letterboxdForm) {
    const apiBaseInput = letterboxdForm.querySelector(
      "input[name='api_base_url']"
    );
    if (
      letterboxd &&
      letterboxd.config &&
      letterboxd.config.api_base_url &&
      apiBaseInput
    ) {
      apiBaseInput.value = letterboxd.config.api_base_url;
    }
    const letterboxdMessage = document.getElementById("letterboxd-message");
    const letterboxdDisconnect = document.getElementById("letterboxd-disconnect");
    const letterboxdConnected = isIntegrationConnected(letterboxd);
    if (letterboxdConnected) {
      if (letterboxdMessage && !letterboxdMessage.textContent) {
        setMessage("letterboxd-message", "Credentials are stored securely.");
      }
      if (letterboxdDisconnect) {
        letterboxdDisconnect.hidden = false;
      }
    } else {
      setMessage("letterboxd-message", "");
      if (letterboxdDisconnect) {
        letterboxdDisconnect.hidden = true;
      }
    }
    applyImportControls("letterboxd", letterboxd);
  }

  const trakt = integrations.find((item) => item.provider === "trakt");
  const traktMessage = document.getElementById("trakt-message");
  const traktConnect = document.getElementById("trakt-connect");
  const traktDisconnect = document.getElementById("trakt-disconnect");
  const traktConnected = isIntegrationConnected(trakt);
  if (traktConnected) {
    const username =
      trakt.config && trakt.config.trakt_username
        ? trakt.config.trakt_username
        : null;
    const label = username
      ? `Connected as ${username}.`
      : "Trakt connection is active.";
    setMessage("trakt-message", label);
    if (traktConnect) {
      traktConnect.hidden = true;
    }
    if (traktDisconnect) {
      traktDisconnect.hidden = false;
    }
  } else {
    setMessage("trakt-message", "");
    if (traktConnect) {
      traktConnect.hidden = false;
    }
    if (traktDisconnect) {
      traktDisconnect.hidden = true;
    }
  }
  applyImportControls("trakt", trakt);

  const simkl = integrations.find((item) => item.provider === "simkl");
  const simklMessage = document.getElementById("simkl-message");
  const simklConnect = document.getElementById("simkl-connect");
  const simklDisconnect = document.getElementById("simkl-disconnect");
  const simklConnected = isIntegrationConnected(simkl);
  if (simklConnected) {
    const username =
      simkl.config && simkl.config.simkl_username ? simkl.config.simkl_username : null;
    const label = username
      ? `Connected as ${username}.`
      : "SIMKL connection is active.";
    setMessage("simkl-message", label);
    if (simklConnect) {
      simklConnect.hidden = true;
    }
    if (simklDisconnect) {
      simklDisconnect.hidden = false;
    }
  } else {
    setMessage("simkl-message", "");
    if (simklConnect) {
      simklConnect.hidden = false;
    }
    if (simklDisconnect) {
      simklDisconnect.hidden = true;
    }
  }
  applyImportControls("simkl", simkl);

  const stremio = integrations.find((item) => item.provider === "stremio");
  const stremioForm = document.getElementById("stremio-form");
  if (stremioForm) {
    const apiBaseInput = stremioForm.querySelector("input[name='api_base_url']");
    if (stremio && stremio.config && stremio.config.api_base_url && apiBaseInput) {
      apiBaseInput.value = stremio.config.api_base_url;
    }
  }
  const stremioMessage = document.getElementById("stremio-message");
  const stremioDisconnect = document.getElementById("stremio-disconnect");
  const stremioConnected = isIntegrationConnected(stremio);
  if (stremioConnected) {
    const name =
      stremio.config && stremio.config.stremio_name ? stremio.config.stremio_name : null;
    const email =
      stremio.config && stremio.config.stremio_email ? stremio.config.stremio_email : null;
    const label = name || email ? `Connected as ${name || email}.` : "Stremio is connected.";
    setMessage("stremio-message", label);
    if (stremioDisconnect) {
      stremioDisconnect.hidden = false;
    }
  } else {
    setMessage("stremio-message", "");
    if (stremioDisconnect) {
      stremioDisconnect.hidden = true;
    }
  }
  applyImportControls("stremio", stremio);

  const importAllButton = document.getElementById("import-all-button");
  if (importAllButton) {
    const hasAnyImports = integrations.some((item) => item.has_secrets);
    importAllButton.disabled = !hasAnyImports;
  }

  renderHistorySyncButtons(integrations);
}

function parseCookieString(cookieHeader) {
  const cookies = {};
  if (!cookieHeader) {
    return cookies;
  }
  cookieHeader.split(";").forEach((part) => {
    const trimmed = part.trim();
    if (!trimmed) {
      return;
    }
    const idx = trimmed.indexOf("=");
    if (idx === -1) {
      return;
    }
    const name = trimmed.slice(0, idx).trim();
    const value = trimmed.slice(idx + 1).trim();
    if (name && value) {
      cookies[name] = value;
    }
  });
  return cookies;
}

function extractCookieHeaders(command) {
  const cookies = {};
  const headerRegex = /(?:-H|--header)\s+(['"])(Cookie:\s*[^'"]+)\1/gi;
  let match = null;
  while ((match = headerRegex.exec(command)) !== null) {
    const headerValue = match[2];
    const cookiePart = headerValue.replace(/^Cookie:\s*/i, "");
    Object.assign(cookies, parseCookieString(cookiePart));
  }
  const cookieRegex = /(?:-b|--cookie)\s+(['"])([^'"]+)\1/gi;
  while ((match = cookieRegex.exec(command)) !== null) {
    const cookieValue = match[2];
    if (cookieValue.startsWith("@")) {
      continue;
    }
    Object.assign(cookies, parseCookieString(cookieValue));
  }
  const inlineCookieRegex = /Cookie:\s*([^'"\n\r]+)/gi;
  while ((match = inlineCookieRegex.exec(command)) !== null) {
    Object.assign(cookies, parseCookieString(match[1]));
  }
  return cookies;
}

function extractDataSegments(command) {
  const segments = [];
  const dataRegex =
    /(?:-d|--data(?:-raw|-urlencode)?|--raw)\s+(['"])(.*?)\1/gi;
  let match = null;
  while ((match = dataRegex.exec(command)) !== null) {
    segments.push(match[2]);
  }
  const dataBareRegex =
    /(?:-d|--data(?:-raw|-urlencode)?|--raw)\s+([^\s]+)/gi;
  while ((match = dataBareRegex.exec(command)) !== null) {
    const value = match[1];
    if (!value.startsWith("-")) {
      segments.push(value);
    }
  }
  return segments;
}

function parseKeyValues(segment) {
  const values = {};
  if (!segment) {
    return values;
  }
  const parts = segment.split("&");
  parts.forEach((part) => {
    const idx = part.indexOf("=");
    if (idx === -1) {
      return;
    }
    const rawKey = part.slice(0, idx);
    const rawValue = part.slice(idx + 1);
    const key = decodeURIComponent(rawKey.replace(/\+/g, " ")).trim();
    const value = decodeURIComponent(rawValue.replace(/\+/g, " ")).trim();
    if (key && value && values[key] === undefined) {
      values[key] = value;
    }
  });
  return values;
}

function extractUrlParams(command) {
  const values = {};
  const urlMatch = command.match(/https?:\/\/[^\s'"]+/i);
  if (!urlMatch) {
    return values;
  }
  try {
    const url = new URL(urlMatch[0]);
    url.searchParams.forEach((value, key) => {
      if (value && values[key] === undefined) {
        values[key] = value;
      }
    });
  } catch (error) {
    return values;
  }
  return values;
}

function extractCredential(command, key) {
  const segments = extractDataSegments(command);
  for (const segment of segments) {
    const trimmed = segment.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === "object" && parsed[key]) {
          return String(parsed[key]);
        }
      } catch (error) {
        // ignore json errors
      }
    }
    const params = parseKeyValues(trimmed);
    if (params[key]) {
      return String(params[key]);
    }
  }
  const urlParams = extractUrlParams(command);
  if (urlParams[key]) {
    return String(urlParams[key]);
  }
  const keyRegex = new RegExp(
    `${key}\\s*(?:==|=|:=)\\s*('[^']*'|"[^"]*"|[^\\s&]+)`,
    "i"
  );
  const match = command.match(keyRegex);
  if (match) {
    return match[1].replace(/^['"]|['"]$/g, "");
  }
  return null;
}

function parseLetterboxdCommand(command) {
  const cookies = extractCookieHeaders(command);
  return {
    client_id: extractCredential(command, "client_id"),
    client_secret: extractCredential(command, "client_secret"),
    refresh_token: extractCredential(command, "refresh_token"),
    cookies: Object.keys(cookies).length ? cookies : null,
  };
}

function handleLetterboxdParse() {
  const commandInput = document.getElementById("letterboxd-command");
  if (!commandInput) {
    return;
  }
  const command = commandInput.value || "";
  if (!command.trim()) {
    setMessage("letterboxd-message", "Paste a curl/httpie command first.", true);
    return;
  }
  const parsed = parseLetterboxdCommand(command);
  const form = document.getElementById("letterboxd-form");
  if (!form) {
    return;
  }
  const fields = [];
  if (parsed.client_id) {
    const input = form.querySelector("input[name='client_id']");
    if (input) {
      input.value = parsed.client_id;
      fields.push("client_id");
    }
  }
  if (parsed.client_secret) {
    const input = form.querySelector("input[name='client_secret']");
    if (input) {
      input.value = parsed.client_secret;
      fields.push("client_secret");
    }
  }
  if (parsed.refresh_token) {
    const input = form.querySelector("input[name='refresh_token']");
    if (input) {
      input.value = parsed.refresh_token;
      fields.push("refresh_token");
    }
  }
  if (parsed.cookies) {
    const input = form.querySelector("textarea[name='cookies']");
    if (input) {
      input.value = JSON.stringify(parsed.cookies, null, 2);
      fields.push("cookies");
    }
  }
  if (!fields.length) {
    setMessage("letterboxd-message", "No Letterboxd fields found.", true);
  } else {
    setMessage(
      "letterboxd-message",
      `Parsed: ${fields.join(", ")}. Review and save.`
    );
  }
}

async function handleLetterboxdSave(data, form) {
  setMessage("letterboxd-message", "");
  const apiBaseUrl = (data.get("api_base_url") || "").trim();
  const clientId = (data.get("client_id") || "").trim();
  const clientSecret = (data.get("client_secret") || "").trim();
  const refreshToken = (data.get("refresh_token") || "").trim();
  const cookiesRaw = (data.get("cookies") || "").trim();
  const payload = {};
  if (apiBaseUrl) {
    payload.api_base_url = apiBaseUrl;
  }
  if (clientId) {
    payload.client_id = clientId;
  }
  if (clientSecret) {
    payload.client_secret = clientSecret;
  }
  if (refreshToken) {
    payload.refresh_token = refreshToken;
  }
  if (cookiesRaw) {
    try {
      const parsed = JSON.parse(cookiesRaw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Cookies JSON must be an object.");
      }
      payload.cookies = parsed;
    } catch (error) {
      setMessage("letterboxd-message", "Invalid cookies JSON.", true);
      return;
    }
  }
  if (!Object.keys(payload).length) {
    setMessage("letterboxd-message", "Enter credentials to save.", true);
    return;
  }
  try {
    await requestJSON("/api/integrations/letterboxd", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const secretInputs = ["client_id", "client_secret", "refresh_token"];
    secretInputs.forEach((name) => {
      const input = form.querySelector(`input[name='${name}']`);
      if (input) {
        input.value = "";
      }
    });
    const cookiesInput = form.querySelector("textarea[name='cookies']");
    if (cookiesInput) {
      cookiesInput.value = "";
    }
    setMessage("letterboxd-message", "Saved.");
    await loadIntegrations();
  } catch (error) {
    setMessage("letterboxd-message", error.message, true);
  }
}

async function handleLetterboxdTest() {
  setMessage("letterboxd-message", "");
  try {
    await requestJSON("/api/integrations/letterboxd/test", {
      method: "POST",
    });
    setMessage("letterboxd-message", "Connection verified.");
  } catch (error) {
    setMessage("letterboxd-message", error.message, true);
  }
}

async function handleLetterboxdDisconnect() {
  setMessage("letterboxd-message", "");
  try {
    await requestJSON("/api/integrations/letterboxd/disconnect", {
      method: "POST",
    });
    setMessage("letterboxd-message", "Letterboxd disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("letterboxd-message", error.message, true);
  }
}

function handleTraktConnect() {
  window.location.href = "/api/integrations/trakt/start";
}

async function handleTraktDisconnect() {
  setMessage("trakt-message", "");
  try {
    await requestJSON("/api/integrations/trakt/disconnect", {
      method: "POST",
    });
    setMessage("trakt-message", "Trakt disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("trakt-message", error.message, true);
  }
}

function handleSimklConnect() {
  window.location.href = "/api/integrations/simkl/start";
}

async function handleSimklDisconnect() {
  setMessage("simkl-message", "");
  try {
    await requestJSON("/api/integrations/simkl/disconnect", {
      method: "POST",
    });
    setMessage("simkl-message", "SIMKL disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("simkl-message", error.message, true);
  }
}

async function handleStremioConnect(data, form) {
  setMessage("stremio-message", "");
  const email = (data.get("email") || "").trim();
  const password = data.get("password") || "";
  const apiBaseUrl = (data.get("api_base_url") || "").trim();
  if (!email) {
    setMessage("stremio-message", "Email is required.", true);
    return;
  }
  if (!password || !password.trim()) {
    setMessage("stremio-message", "Password is required.", true);
    return;
  }
  const payload = { email, password };
  if (apiBaseUrl) {
    payload.api_base_url = apiBaseUrl;
  }
  try {
    await requestJSON("/api/integrations/stremio/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const passwordInput = form.querySelector("input[name='password']");
    if (passwordInput) {
      passwordInput.value = "";
    }
    setMessage("stremio-message", "Connected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("stremio-message", error.message, true);
  }
}

async function handleStremioDisconnect() {
  setMessage("stremio-message", "");
  try {
    await requestJSON("/api/integrations/stremio/disconnect", {
      method: "POST",
    });
    setMessage("stremio-message", "Stremio disconnected.");
    await loadIntegrations();
  } catch (error) {
    setMessage("stremio-message", error.message, true);
  }
}

async function handleImportScheduleSave(provider, data) {
  setMessage(`${provider}-import-message`, "");
  const rawInterval = data.get("import_interval");
  const intervalSeconds = parseIntervalSeconds(rawInterval);
  if (rawInterval && intervalSeconds === null) {
    setMessage(
      `${provider}-import-message`,
      "Select a valid import schedule.",
      true
    );
    return;
  }
  try {
    await requestJSON(`/api/integrations/${provider}/import/schedule`, {
      method: "POST",
      body: JSON.stringify({ interval_seconds: intervalSeconds }),
    });
    setMessage(`${provider}-import-message`, "Schedule saved.");
    await loadIntegrations();
  } catch (error) {
    setMessage(`${provider}-import-message`, error.message, true);
  }
}

async function handleImportNow(provider) {
  setMessage(`${provider}-import-message`, "Requesting import...");
  try {
    await requestJSON(`/api/integrations/${provider}/import/now`, {
      method: "POST",
    });
    setMessage(`${provider}-import-message`, "Import requested.");
    await loadIntegrations();
  } catch (error) {
    setMessage(`${provider}-import-message`, error.message, true);
  }
}

async function handleImportAll() {
  const button = document.getElementById("import-all-button");
  if (button) {
    button.disabled = true;
  }
  setMessage("import-all-message", "Requesting import...");
  try {
    const response = await requestJSON("/api/integrations/import/all", {
      method: "POST",
    });
    const providers = response && response.providers ? response.providers : [];
    const label = providers.length
      ? `Import queued: ${providers.join(", ")}.`
      : "Import requested.";
    setMessage("import-all-message", label);
    await loadIntegrations();
  } catch (error) {
    setMessage("import-all-message", error.message, true);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

async function loadSettings() {
  const form = document.getElementById("settings-form");
  if (!form) {
    return;
  }
  const data = await requestJSON("/api/settings");
  const includeAdultInput = form.querySelector(
    "input[name='include_adult_in_search']"
  );
  if (includeAdultInput) {
    includeAdultInput.checked = !!data.include_adult_in_search;
  }
}

async function handleSettingsSave(data) {
  setMessage("settings-message", "");
  const includeAdult = data.get("include_adult_in_search") === "on";
  const payload = {
    include_adult_in_search: includeAdult,
  };
  try {
    const updated = await requestJSON("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("settings-message", "Saved.");
    if (updated) {
      await loadSettings();
    }
  } catch (error) {
    setMessage("settings-message", error.message, true);
  }
}

async function loadMetadataProviders() {
  const tmdbForm = document.getElementById("tmdb-form");
  const tvdbForm = document.getElementById("tvdb-form");
  const tvmazeForm = document.getElementById("tvmaze-form");
  const imdbForm = document.getElementById("imdb-form");
  const kitsuForm = document.getElementById("kitsu-form");
  const myanimelistForm = document.getElementById("myanimelist-form");
  if (!tmdbForm && !tvdbForm && !kitsuForm && !tvmazeForm && !imdbForm && !myanimelistForm) {
    return;
  }
  const data = await requestJSON("/api/metadata/providers");
  const providers = data && data.providers ? data.providers : [];
  const tmdb = providers.find((item) => item.provider === "tmdb") || {
    enabled: false,
    config: {},
  };
  const tvdb = providers.find((item) => item.provider === "tvdb") || {
    enabled: false,
    config: {},
  };
  const tvmaze = providers.find((item) => item.provider === "tvmaze") || {
    enabled: false,
    config: {},
  };
  const imdb = providers.find((item) => item.provider === "imdb") || {
    enabled: false,
    config: {},
  };
  const kitsu = providers.find((item) => item.provider === "kitsu") || {
    enabled: false,
    config: {},
  };
  const myanimelist = providers.find((item) => item.provider === "myanimelist") || {
    enabled: false,
    config: {},
  };

  if (tmdbForm) {
    const enabledInput = tmdbForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!tmdb.enabled;
    }
    const languageInput = tmdbForm.querySelector("input[name='language']");
    const regionInput = tmdbForm.querySelector("input[name='region']");
    if (languageInput) {
      languageInput.value =
        tmdb.config && tmdb.config.language ? tmdb.config.language : "";
    }
    if (regionInput) {
      regionInput.value = tmdb.config && tmdb.config.region ? tmdb.config.region : "";
    }
    if (tmdb.has_credentials) {
      setMessage("tmdb-message", "API key is stored securely.");
    } else {
      setMessage("tmdb-message", "");
    }
  }

  if (tvdbForm) {
    const enabledInput = tvdbForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!tvdb.enabled;
    }
    const languageInput = tvdbForm.querySelector("input[name='language']");
    if (languageInput) {
      languageInput.value =
        tvdb.config && tvdb.config.language ? tvdb.config.language : "";
    }
    if (tvdb.has_credentials) {
      setMessage("tvdb-message", "Credentials are stored securely.");
    } else {
      setMessage("tvdb-message", "");
    }
  }

  if (tvmazeForm) {
    const enabledInput = tvmazeForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!tvmaze.enabled;
    }
    if (tvmaze.enabled) {
      setMessage("tvmaze-message", "No API key required.");
    } else {
      setMessage("tvmaze-message", "");
    }
  }

  if (imdbForm) {
    const enabledInput = imdbForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!imdb.enabled;
    }
    if (imdb.enabled) {
      setMessage("imdb-message", "No API key required.");
    } else {
      setMessage("imdb-message", "");
    }
  }

  if (kitsuForm) {
    const enabledInput = kitsuForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!kitsu.enabled;
    }
    const languageInput = kitsuForm.querySelector("input[name='language']");
    if (languageInput) {
      languageInput.value =
        kitsu.config && kitsu.config.language ? kitsu.config.language : "";
    }
    if (kitsu.enabled) {
      setMessage("kitsu-message", "No API key required.");
    } else {
      setMessage("kitsu-message", "");
    }
  }

  if (myanimelistForm) {
    const enabledInput = myanimelistForm.querySelector("input[name='enabled']");
    if (enabledInput) {
      enabledInput.checked = !!myanimelist.enabled;
    }
    if (myanimelist.enabled) {
      setMessage("myanimelist-message", "No API key required.");
    } else {
      setMessage("myanimelist-message", "");
    }
  }
}

async function handleTmdbSave(data, form) {
  setMessage("tmdb-message", "");
  const enabled = data.get("enabled") === "on";
  const apiKeyRaw = data.get("api_key");
  const language = (data.get("language") || "").trim();
  const region = (data.get("region") || "").trim();
  const payload = {
    enabled,
    language: language || null,
    region: region || null,
  };
  if (apiKeyRaw !== null && apiKeyRaw !== undefined) {
    const apiKey = apiKeyRaw.trim();
    if (apiKey) {
      payload.api_key = apiKey;
    }
  }
  try {
    await requestJSON("/api/metadata/providers/tmdb", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const apiKeyInput = form.querySelector("input[name='api_key']");
    if (apiKeyInput) {
      apiKeyInput.value = "";
    }
    setMessage("tmdb-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("tmdb-message", error.message, true);
  }
}

async function handleTvdbSave(data, form) {
  setMessage("tvdb-message", "");
  const enabled = data.get("enabled") === "on";
  const apiKeyRaw = data.get("api_key");
  const pinRaw = data.get("pin");
  const language = (data.get("language") || "").trim();
  const payload = {
    enabled,
    language: language || null,
  };
  if (apiKeyRaw !== null && apiKeyRaw !== undefined) {
    const apiKey = apiKeyRaw.trim();
    if (apiKey) {
      payload.api_key = apiKey;
    }
  }
  if (pinRaw !== null && pinRaw !== undefined) {
    const pin = pinRaw.trim();
    if (pin) {
      payload.pin = pin;
    }
  }
  try {
    await requestJSON("/api/metadata/providers/tvdb", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const apiKeyInput = form.querySelector("input[name='api_key']");
    if (apiKeyInput) {
      apiKeyInput.value = "";
    }
    const pinInput = form.querySelector("input[name='pin']");
    if (pinInput) {
      pinInput.value = "";
    }
    setMessage("tvdb-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("tvdb-message", error.message, true);
  }
}

async function handleKitsuSave(data) {
  setMessage("kitsu-message", "");
  const enabled = data.get("enabled") === "on";
  const language = (data.get("language") || "").trim();
  const payload = {
    enabled,
    language: language || null,
  };
  try {
    await requestJSON("/api/metadata/providers/kitsu", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("kitsu-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("kitsu-message", error.message, true);
  }
}

async function handleTvmazeSave(data) {
  setMessage("tvmaze-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/tvmaze", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("tvmaze-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("tvmaze-message", error.message, true);
  }
}

async function handleImdbSave(data) {
  setMessage("imdb-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/imdb", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("imdb-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("imdb-message", error.message, true);
  }
}

async function handleMyAnimeListSave(data) {
  setMessage("myanimelist-message", "");
  const enabled = data.get("enabled") === "on";
  const payload = { enabled };
  try {
    await requestJSON("/api/metadata/providers/myanimelist", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("myanimelist-message", "Saved.");
    await loadMetadataProviders();
  } catch (error) {
    setMessage("myanimelist-message", error.message, true);
  }
}

function clearLookupTimer() {
  if (lookupState.timer) {
    window.clearTimeout(lookupState.timer);
    lookupState.timer = null;
  }
}

function resetLookupUI() {
  const resultsCard = document.getElementById("lookup-results");
  const candidatesEl = document.getElementById("candidate-list");
  if (resultsCard) {
    resultsCard.hidden = true;
  }
  if (candidatesEl) {
    candidatesEl.innerHTML = "";
  }
  lookupState.id = null;
  lookupState.candidates = [];
  resetEpisodePicker();
  setMessage("lookup-message", "");
  setMessage("confirm-message", "");
}

function resetEpisodePicker() {
  const picker = document.getElementById("episode-picker");
  const seasonSelect = document.getElementById("season-select");
  const episodeSelect = document.getElementById("episode-select");
  if (picker) {
    picker.hidden = true;
  }
  if (seasonSelect) {
    seasonSelect.innerHTML = "";
    seasonSelect.disabled = true;
  }
  if (episodeSelect) {
    episodeSelect.innerHTML = "";
    episodeSelect.disabled = true;
  }
  episodeState.tmdbId = null;
  episodeState.seasonNumber = null;
  setMessage("episode-message", "");
}

async function handleLookupSubmit(data) {
  resetLookupUI();
  clearLookupTimer();
  const query = (data.get("query") || "").trim();
  const scopeRaw = (data.get("search_scope") || "all").toLowerCase();
  const searchScope = ["all", "movie", "tv", "anime"].includes(scopeRaw)
    ? scopeRaw
    : "all";
  if (!query) {
    setMessage("lookup-message", "Enter a title or ID to search.", true);
    return;
  }
  try {
    setMessage("lookup-message", "Searching...");
    const response = await requestJSON("/api/metadata/lookup", {
      method: "POST",
      body: JSON.stringify({ query, search_scope: searchScope }),
    });
    lookupState.id = response.lookup_id;
    await pollLookupStatus(response.lookup_id);
  } catch (error) {
    setMessage("lookup-message", error.message, true);
  }
}

async function pollLookupStatus(lookupId) {
  try {
    const data = await requestJSON(`/api/metadata/lookup/${lookupId}`);
    if (data.status === "completed") {
      renderCandidates(data.candidates || []);
      return;
    }
    if (data.status === "failed") {
      setMessage("lookup-message", data.error || "Lookup failed.", true);
      return;
    }
    lookupState.timer = window.setTimeout(() => pollLookupStatus(lookupId), 1500);
  } catch (error) {
    setMessage("lookup-message", error.message, true);
  }
}

function renderCandidates(candidates) {
  const resultsCard = document.getElementById("lookup-results");
  const candidatesEl = document.getElementById("candidate-list");
  if (!resultsCard || !candidatesEl) {
    return;
  }
  lookupState.candidates = candidates;
  candidatesEl.innerHTML = "";
  if (!candidates.length) {
    candidatesEl.textContent = "No matches found.";
    resetEpisodePicker();
  } else {
    candidates.forEach((candidate, index) => {
      const label = document.createElement("label");
      label.className = "candidate-option";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "candidate_id";
      input.value = candidate.id;
      if (index === 0) {
        input.checked = true;
      }

      const poster = document.createElement("img");
      poster.className = "candidate-poster";
      if (candidate.poster_url) {
        poster.src = candidate.poster_url;
        poster.alt = `${candidate.title} poster`;
        poster.loading = "lazy";
      } else {
        poster.alt = "";
      }

      const meta = document.createElement("div");
      meta.className = "candidate-meta";
      const title = document.createElement("h3");
      title.textContent = candidate.title;
      const detail = document.createElement("p");
      const year = candidate.year ? candidate.year : "Year unknown";
      const mediaType = formatMediaType(candidate.media_type);
      const providerLabel = formatProviderLabel(
        Array.isArray(candidate.providers) && candidate.providers.length
          ? candidate.providers
          : candidate.provider,
      );
      const detailParts = [`${year}`, mediaType];
      if (providerLabel) {
        detailParts.push(providerLabel);
      }
      const idParts = [];
      if (candidate.imdb_id) {
        idParts.push(candidate.imdb_id);
      }
      if (candidate.tmdb_id) {
        idParts.push(`TMDB ${candidate.tmdb_id}`);
      }
      if (candidate.tvdb_id) {
        idParts.push(`TVDB ${candidate.tvdb_id}`);
      }
      if (candidate.tvmaze_id) {
        idParts.push(`TVMaze ${candidate.tvmaze_id}`);
      }
      if (candidate.kitsu_id) {
        idParts.push(`Kitsu ${candidate.kitsu_id}`);
      }
      if (candidate.myanimelist_id) {
        idParts.push(`MAL ${candidate.myanimelist_id}`);
      }
      if (idParts.length) {
        detailParts.push(...idParts);
      }
      detail.textContent = detailParts.join(" · ");

      meta.appendChild(title);
      meta.appendChild(detail);
      label.appendChild(input);
      label.appendChild(poster);
      label.appendChild(meta);
      candidatesEl.appendChild(label);
    });
    handleCandidateSelection();
  }
  resultsCard.hidden = false;
  setMessage("lookup-message", "");
}

function getSelectedCandidate() {
  const candidateId = getSelectedCandidateId();
  if (!candidateId) {
    return null;
  }
  return lookupState.candidates.find((entry) => entry.id === candidateId) || null;
}

function getSelectedCandidateId() {
  const input = document.querySelector("input[name='candidate_id']:checked");
  return input ? input.value : null;
}

async function handleCandidateSelection() {
  const candidate = getSelectedCandidate();
  if (!candidate || candidate.media_type !== "tv") {
    resetEpisodePicker();
    return;
  }
  const tmdbId = candidate.tmdb_id;
  if (!tmdbId) {
    resetEpisodePicker();
    const picker = document.getElementById("episode-picker");
    if (picker) {
      picker.hidden = false;
    }
    setMessage(
      "episode-message",
      "Episode lookup requires a TMDB-backed result. Try another match.",
      true,
    );
    return;
  }
  await loadSeasons(tmdbId);
}

async function loadSeasons(tmdbId) {
  const picker = document.getElementById("episode-picker");
  const seasonSelect = document.getElementById("season-select");
  const episodeSelect = document.getElementById("episode-select");
  if (!picker || !seasonSelect || !episodeSelect) {
    return;
  }
  picker.hidden = false;
  seasonSelect.disabled = true;
  episodeSelect.disabled = true;
  episodeSelect.innerHTML = "";
  setMessage("episode-message", "Loading seasons...");
  episodeState.tmdbId = tmdbId;
  try {
    const seasons = await requestJSON(`/api/metadata/tv/tmdb/${tmdbId}/seasons`);
    if (episodeState.tmdbId !== tmdbId) {
      return;
    }
    const sorted = (seasons || []).slice().sort((a, b) => a.season_number - b.season_number);
    seasonSelect.innerHTML = "";
    if (!sorted.length) {
      setMessage("episode-message", "No seasons found.", true);
      seasonSelect.disabled = true;
      return;
    }
    sorted.forEach((season) => {
      const option = document.createElement("option");
      option.value = String(season.season_number);
      const label = season.name || `Season ${season.season_number}`;
      const count = season.episode_count ? ` · ${season.episode_count} eps` : "";
      option.textContent = `${label}${count}`;
      seasonSelect.appendChild(option);
    });
    const defaultSeason = sorted[sorted.length - 1];
    seasonSelect.value = String(defaultSeason.season_number);
    seasonSelect.disabled = false;
    episodeState.seasonNumber = defaultSeason.season_number;
    setMessage("episode-message", "");
    await loadEpisodes(tmdbId, defaultSeason.season_number);
  } catch (error) {
    setMessage("episode-message", error.message, true);
    seasonSelect.disabled = true;
    episodeSelect.disabled = true;
  }
}

async function loadEpisodes(tmdbId, seasonNumber) {
  const episodeSelect = document.getElementById("episode-select");
  if (!episodeSelect) {
    return;
  }
  episodeSelect.disabled = true;
  episodeSelect.innerHTML = "";
  setMessage("episode-message", "Loading episodes...");
  episodeState.seasonNumber = seasonNumber;
  try {
    const episodes = await requestJSON(
      `/api/metadata/tv/tmdb/${tmdbId}/seasons/${seasonNumber}/episodes`,
    );
    if (episodeState.tmdbId !== tmdbId || episodeState.seasonNumber !== seasonNumber) {
      return;
    }
    const sorted = (episodes || []).slice().sort((a, b) => a.episode_number - b.episode_number);
    episodeSelect.innerHTML = "";
    if (!sorted.length) {
      setMessage("episode-message", "No episodes found.", true);
      episodeSelect.disabled = true;
      return;
    }
    sorted.forEach((episode) => {
      const option = document.createElement("option");
      option.value = String(episode.episode_number);
      option.dataset.episodeTitle = episode.title || "";
      option.dataset.episodeTmdbId = episode.tmdb_id || "";
      const label = `E${String(episode.episode_number).padStart(2, "0")}`;
      option.textContent = episode.title ? `${label} · ${episode.title}` : label;
      episodeSelect.appendChild(option);
    });
    episodeSelect.disabled = false;
    setMessage("episode-message", "");
  } catch (error) {
    setMessage("episode-message", error.message, true);
    episodeSelect.disabled = true;
  }
}

function getEpisodeSelection() {
  const seasonSelect = document.getElementById("season-select");
  const episodeSelect = document.getElementById("episode-select");
  if (!seasonSelect || !episodeSelect) {
    return null;
  }
  if (seasonSelect.disabled || episodeSelect.disabled) {
    return null;
  }
  const seasonNumber = Number(seasonSelect.value);
  const episodeNumber = Number(episodeSelect.value);
  if (Number.isNaN(seasonNumber) || Number.isNaN(episodeNumber)) {
    return null;
  }
  const selectedOption = episodeSelect.selectedOptions[0];
  return {
    seasonNumber,
    episodeNumber,
    title: selectedOption ? selectedOption.dataset.episodeTitle : "",
    tmdbId: selectedOption ? selectedOption.dataset.episodeTmdbId : "",
  };
}

async function handleLookupConfirm(data) {
  setMessage("confirm-message", "");
  if (!lookupState.candidates.length) {
    setMessage("confirm-message", "Search for a title first.", true);
    return;
  }
  const candidateId = data.get("candidate_id");
  if (!candidateId) {
    setMessage("confirm-message", "Select a result to confirm.", true);
    return;
  }
  const candidate = lookupState.candidates.find((entry) => entry.id === candidateId);
  if (!candidate) {
    setMessage("confirm-message", "Selected result is no longer available.", true);
    return;
  }
  const watchedRaw = (data.get("watched_at") || "").trim();
  const payload = buildHistoryPayload(candidate);
  if (!hasHistoryIds(payload)) {
    setMessage("confirm-message", "Selected result is missing external IDs.", true);
    return;
  }
  if (candidate.media_type === "tv") {
    const episodeSelection = getEpisodeSelection();
    if (!episodeSelection) {
      setMessage("confirm-message", "Select a season and episode first.", true);
      return;
    }
    payload.season_number = episodeSelection.seasonNumber;
    payload.episode_number = episodeSelection.episodeNumber;
    if (episodeSelection.title) {
      payload.episode_title = episodeSelection.title;
    }
    if (episodeSelection.tmdbId) {
      payload.episode_tmdb_id = episodeSelection.tmdbId;
    }
  }
  if (watchedRaw) {
    const watchedDate = new Date(watchedRaw);
    if (!Number.isNaN(watchedDate.valueOf())) {
      payload.watched_at = watchedDate.toISOString();
    }
  }
  const ratingValue = parseRatingValue(data.get("rating"));
  if (ratingValue !== null) {
    payload.rating = ratingValue;
  }
  try {
    await requestJSON("/api/history/items", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage("confirm-message", "Watched saved.");
    const confirmForm = document.getElementById("confirm-form");
    if (confirmForm) {
      confirmForm.reset();
    }
    await loadHistory();
  } catch (error) {
    setMessage("confirm-message", error.message, true);
  }
}

function buildHistoryPayload(candidate) {
  const payload = {
    media_type: candidate.media_type || "movie",
  };
  if (candidate.imdb_id) {
    payload.imdb_id = candidate.imdb_id;
  }
  if (candidate.tmdb_id) {
    payload.tmdb_id = candidate.tmdb_id;
  }
  if (candidate.tvdb_id) {
    payload.tvdb_id = candidate.tvdb_id;
  }
  if (candidate.tvmaze_id) {
    payload.tvmaze_id = candidate.tvmaze_id;
  }
  if (candidate.kitsu_id) {
    payload.kitsu_id = candidate.kitsu_id;
  }
  if (candidate.myanimelist_id) {
    payload.myanimelist_id = candidate.myanimelist_id;
  }
  if (candidate.title) {
    payload.title = candidate.title;
  }
  if (candidate.year) {
    payload.year = candidate.year;
  }
  if (candidate.poster_url) {
    payload.poster_url = candidate.poster_url;
  }
  return payload;
}

function hasHistoryIds(payload) {
  return Boolean(
    payload.imdb_id ||
      payload.tmdb_id ||
      payload.tvdb_id ||
      payload.tvmaze_id ||
      payload.kitsu_id ||
      payload.myanimelist_id,
  );
}

function formatDateTimeInput(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "";
  }
  const pad = (num) => String(num).padStart(2, "0");
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function parseDateTimeInput(value) {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return null;
  }
  return date.toISOString();
}

function parseRatingValue(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const raw = String(value).trim();
  if (!raw) {
    return null;
  }
  const rating = Number(raw);
  if (!Number.isFinite(rating)) {
    return null;
  }
  return rating;
}

function formatMediaType(value) {
  if (value === "tv") {
    return "TV";
  }
  if (value === "anime") {
    return "Anime";
  }
  return "Movie";
}

function formatSeasonEpisode(seasonNumber, episodeNumber) {
  if (seasonNumber === null || seasonNumber === undefined) {
    return "";
  }
  if (episodeNumber === null || episodeNumber === undefined) {
    return "";
  }
  const pad = (value) => String(value).padStart(2, "0");
  return `S${pad(seasonNumber)}E${pad(episodeNumber)}`;
}

function formatRating(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const rating = Number(value);
  if (!Number.isFinite(rating)) {
    return "";
  }
  return rating === 1 ? "1 star" : `${rating} stars`;
}

function buildRatingSelect(currentValue) {
  const select = document.createElement("select");
  select.className = "rating-select";
  select.setAttribute("aria-label", "Rating");
  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = "No rating";
  select.appendChild(emptyOption);
  const options = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];
  options.forEach((value) => {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = value === 1 ? "1 star" : `${value} stars`;
    select.appendChild(option);
  });
  if (currentValue !== null && currentValue !== undefined) {
    select.value = String(currentValue);
  }
  return select;
}

function formatProviderLabel(value) {
  if (!value) {
    return "";
  }
  if (Array.isArray(value)) {
    const labels = value.map((entry) => formatProviderLabel(entry)).filter(Boolean);
    return labels.join(" + ");
  }
  const normalized = value.toLowerCase();
  if (normalized === "tmdb") {
    return "TMDB";
  }
  if (normalized === "tvdb") {
    return "TVDB";
  }
  if (normalized === "tvmaze") {
    return "TVMaze";
  }
  if (normalized === "imdb") {
    return "IMDb";
  }
  if (normalized === "kitsu") {
    return "Kitsu";
  }
  if (normalized === "myanimelist") {
    return "MyAnimeList";
  }
  if (normalized === "local") {
    return "Local cache";
  }
  return normalized.toUpperCase();
}

function formatIntegrationName(value) {
  if (!value) {
    return "";
  }
  const normalized = value.toLowerCase();
  if (normalized === "simkl") {
    return "SIMKL";
  }
  if (normalized === "trakt") {
    return "Trakt";
  }
  if (normalized === "letterboxd") {
    return "Letterboxd";
  }
  if (normalized === "stremio") {
    return "Stremio";
  }
  return normalized.toUpperCase();
}

let historyUiBound = false;
const historySelectionState = {
  items: [],
  selectedIds: new Set(),
};

function updateHistoryBulkControls() {
  const selectAll = document.getElementById("history-select-all");
  const deleteButton = document.getElementById("history-delete-selected");
  const total = historySelectionState.items.length;
  const selectedCount = historySelectionState.selectedIds.size;

  if (selectAll) {
    selectAll.disabled = total === 0;
    if (total === 0 || selectedCount === 0) {
      selectAll.checked = false;
      selectAll.indeterminate = false;
    } else if (selectedCount === total) {
      selectAll.checked = true;
      selectAll.indeterminate = false;
    } else {
      selectAll.checked = false;
      selectAll.indeterminate = true;
    }
  }

  if (deleteButton) {
    deleteButton.disabled = selectedCount === 0;
    deleteButton.textContent = selectedCount
      ? `Delete selected (${selectedCount})`
      : "Delete selected";
  }

  updateHistorySyncControls();
}

function updateHistorySyncControls() {
  const container = document.getElementById("history-sync-actions");
  if (!container) {
    return;
  }
  const hasItems = historySelectionState.items.length > 0;
  container.querySelectorAll("button[data-history-sync]").forEach((button) => {
    button.disabled = !hasItems;
  });
}

function resetHistorySelection(items) {
  historySelectionState.items = items;
  historySelectionState.selectedIds.clear();
  updateHistoryBulkControls();
}

function bindHistoryUi() {
  if (historyUiBound) {
    return;
  }
  const modal = document.getElementById("metadata-modal");
  if (!modal) {
    return;
  }
  modal.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", closeMetadataModal);
  });
  document.addEventListener("click", (event) => {
    if (event.target && event.target.closest(".history-actions")) {
      return;
    }
    closeHistoryMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeHistoryMenus();
      closeMetadataModal();
    }
  });
  const selectAll = document.getElementById("history-select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      const shouldSelect = selectAll.checked;
      historySelectionState.selectedIds.clear();
      document.querySelectorAll("input[data-history-select]").forEach((input) => {
        input.checked = shouldSelect;
        if (shouldSelect) {
          historySelectionState.selectedIds.add(input.value);
        }
      });
      updateHistoryBulkControls();
    });
  }
  const deleteSelectedButton = document.getElementById("history-delete-selected");
  if (deleteSelectedButton) {
    deleteSelectedButton.addEventListener("click", async () => {
      const selectedIds = Array.from(historySelectionState.selectedIds);
      if (!selectedIds.length) {
        return;
      }
      const deleteIntegrationsToggle = document.getElementById(
        "history-delete-integrations"
      );
      const deleteIntegrations =
        deleteIntegrationsToggle && deleteIntegrationsToggle.checked;
      const confirmed = window.confirm(
        deleteIntegrations
          ? `Delete ${selectedIds.length} selected items from history and ` +
              "connected integrations? This cannot be undone."
          : `Delete ${selectedIds.length} selected items from history?`
      );
      if (!confirmed) {
        return;
      }
      try {
        setMessage("history-message", "Deleting selected...");
        const data = await requestJSON("/api/history/items/bulk-delete", {
          method: "POST",
          body: JSON.stringify({
            watched_ids: selectedIds,
            delete_integrations: deleteIntegrations,
          }),
        });
        const deletedCount = data && typeof data.deleted === "number" ? data.deleted : 0;
        setMessage("history-message", `Deleted ${deletedCount} entries.`);
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });
  }
  historyUiBound = true;
}

function renderHistorySyncButtons(integrations) {
  const container = document.getElementById("history-sync-actions");
  if (!container) {
    return;
  }
  const note = document.getElementById("history-sync-note");
  container.innerHTML = "";
  const supported = new Set(["letterboxd", "trakt", "simkl", "stremio"]);
  const enabled = (integrations || []).filter(
    (integration) =>
      integration &&
      supported.has(integration.provider) &&
      isIntegrationConnected(integration)
  );
  if (!enabled.length) {
    container.hidden = true;
    if (note) {
      note.hidden = true;
    }
    return;
  }
  enabled.forEach((integration) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    button.dataset.historySync = integration.provider;
    const label = formatIntegrationName(integration.provider);
    button.textContent = `Sync to ${label}`;
    button.addEventListener("click", () => handleHistorySync(integration.provider));
    container.appendChild(button);
  });
  container.hidden = false;
  if (note) {
    note.hidden = false;
  }
  updateHistorySyncControls();
}

async function handleHistorySync(provider) {
  const label = formatIntegrationName(provider);
  const totalItems = historySelectionState.items.length;
  if (!totalItems) {
    setMessage("history-message", "No history items to sync.", true);
    return;
  }
  const selectedIds = Array.from(historySelectionState.selectedIds);
  const hasSelection = selectedIds.length > 0;
  const count = hasSelection ? selectedIds.length : totalItems;
  const prompt = hasSelection
    ? `Sync ${count} selected item${count === 1 ? "" : "s"} to ${label}?`
    : `Sync all ${count} item${count === 1 ? "" : "s"} to ${label}?`;
  if (!window.confirm(prompt)) {
    return;
  }
  const payload = { provider };
  if (hasSelection) {
    payload.watched_ids = selectedIds;
  }
  try {
    setMessage("history-message", "Queueing sync...");
    const data = await requestJSON("/api/history/items/sync", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const requested =
      data && typeof data.requested === "number" ? data.requested : count;
    setMessage(
      "history-message",
      `Sync requested for ${requested} item${requested === 1 ? "" : "s"} to ${label}.`
    );
    await loadHistory();
  } catch (error) {
    setMessage("history-message", error.message, true);
  }
}

function closeHistoryMenus() {
  document.querySelectorAll("[data-menu-panel].is-open").forEach((panel) => {
    panel.classList.remove("is-open");
  });
  document.querySelectorAll("[data-menu-button]").forEach((button) => {
    button.setAttribute("aria-expanded", "false");
  });
}

function formatMetadataDate(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "—";
  }
  return date.toLocaleString();
}

function formatMetadataValue(value) {
  if (!value) {
    return "—";
  }
  return String(value);
}

function renderMetadataSection(title, rows) {
  const section = document.createElement("section");
  section.className = "metadata-section";
  const header = document.createElement("h3");
  header.textContent = title;
  section.appendChild(header);
  const list = document.createElement("dl");
  list.className = "metadata-grid";
  rows.forEach((row) => {
    const term = document.createElement("dt");
    term.textContent = row.label;
    const desc = document.createElement("dd");
    desc.textContent = row.value;
    list.appendChild(term);
    list.appendChild(desc);
  });
  section.appendChild(list);
  return section;
}

function openMetadataModal(item) {
  const modal = document.getElementById("metadata-modal");
  const body = document.getElementById("metadata-body");
  const title = document.getElementById("metadata-title");
  if (!modal || !body || !title) {
    return;
  }
  const metadata = item.metadata || {};
  const ids = {
    imdb_id: (metadata.ids && metadata.ids.imdb_id) || item.imdb_id,
    tmdb_id: (metadata.ids && metadata.ids.tmdb_id) || item.tmdb_id,
    tvdb_id: (metadata.ids && metadata.ids.tvdb_id) || item.tvdb_id,
    tvmaze_id: (metadata.ids && metadata.ids.tvmaze_id) || item.tvmaze_id,
    kitsu_id: (metadata.ids && metadata.ids.kitsu_id) || item.kitsu_id,
    myanimelist_id:
      (metadata.ids && metadata.ids.myanimelist_id) || item.myanimelist_id,
  };
  const episodeIds = {
    imdb_id:
      (metadata.episode_ids && metadata.episode_ids.imdb_id) ||
      item.episode_imdb_id,
    tmdb_id:
      (metadata.episode_ids && metadata.episode_ids.tmdb_id) ||
      item.episode_tmdb_id,
    tvdb_id:
      (metadata.episode_ids && metadata.episode_ids.tvdb_id) ||
      item.episode_tvdb_id,
    tvmaze_id:
      (metadata.episode_ids && metadata.episode_ids.tvmaze_id) ||
      item.episode_tvmaze_id,
  };
  title.textContent = `Metadata for ${item.title}`;
  body.innerHTML = "";

  const systemRows = [
    { label: "Watched entry ID", value: formatMetadataValue(item.id) },
    {
      label: "Media item ID",
      value: formatMetadataValue(metadata.media_item_id),
    },
  ];
  if (item.media_type === "tv") {
    systemRows.push({
      label: "Episode item ID",
      value: formatMetadataValue(metadata.episode_item_id),
    });
  }
  body.appendChild(renderMetadataSection("Library IDs", systemRows));

  const externalRows = [
    { label: "IMDb", value: formatMetadataValue(ids.imdb_id) },
    { label: "TMDB", value: formatMetadataValue(ids.tmdb_id) },
    { label: "TVDB", value: formatMetadataValue(ids.tvdb_id) },
    { label: "TVMaze", value: formatMetadataValue(ids.tvmaze_id) },
    { label: "Kitsu", value: formatMetadataValue(ids.kitsu_id) },
    { label: "MyAnimeList", value: formatMetadataValue(ids.myanimelist_id) },
  ];
  body.appendChild(renderMetadataSection("External IDs", externalRows));

  if (item.media_type === "tv") {
    const episodeRows = [
      { label: "Episode IMDb", value: formatMetadataValue(episodeIds.imdb_id) },
      { label: "Episode TMDB", value: formatMetadataValue(episodeIds.tmdb_id) },
      { label: "Episode TVDB", value: formatMetadataValue(episodeIds.tvdb_id) },
      {
        label: "Episode TVMaze",
        value: formatMetadataValue(episodeIds.tvmaze_id),
      },
    ];
    body.appendChild(renderMetadataSection("Episode IDs", episodeRows));
  }

  const timestampRows = [
    {
      label: "Watched entry created",
      value: formatMetadataDate(metadata.watched_created_at),
    },
    {
      label: "Media metadata created",
      value: formatMetadataDate(metadata.media_created_at),
    },
    {
      label: "Media metadata updated",
      value: formatMetadataDate(metadata.media_updated_at),
    },
  ];
  if (metadata.episode_created_at || metadata.episode_updated_at) {
    timestampRows.push(
      {
        label: "Episode metadata created",
        value: formatMetadataDate(metadata.episode_created_at),
      },
      {
        label: "Episode metadata updated",
        value: formatMetadataDate(metadata.episode_updated_at),
      },
    );
  }
  timestampRows.push(
    {
      label: "First sync attempt",
      value: formatMetadataDate(metadata.first_sync_at),
    },
    {
      label: "Last sync update",
      value: formatMetadataDate(metadata.last_sync_at),
    },
  );
  body.appendChild(renderMetadataSection("Timestamps", timestampRows));

  modal.removeAttribute("hidden");
}

function closeMetadataModal() {
  const modal = document.getElementById("metadata-modal");
  if (!modal) {
    return;
  }
  modal.setAttribute("hidden", "");
}

async function loadHistory() {
  const container = document.getElementById("history-list");
  if (!container) {
    return;
  }
  bindHistoryUi();
  const clearButton = document.getElementById("history-clear");
  const data = await requestJSON("/api/history/items");
  const items = data && data.items ? data.items : [];
  resetHistorySelection(items);
  if (clearButton) {
    clearButton.disabled = !items.length;
  }
  if (!items.length) {
    container.textContent = "No watched titles yet.";
    return;
  }
  container.innerHTML = "";
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "history-card";
    const selectWrap = document.createElement("label");
    selectWrap.className = "history-select";
    const selectInput = document.createElement("input");
    selectInput.type = "checkbox";
    selectInput.value = item.id;
    selectInput.setAttribute("aria-label", `Select ${item.title}`);
    selectInput.setAttribute("data-history-select", "true");
    selectInput.addEventListener("change", () => {
      if (selectInput.checked) {
        historySelectionState.selectedIds.add(item.id);
      } else {
        historySelectionState.selectedIds.delete(item.id);
      }
      updateHistoryBulkControls();
    });
    selectWrap.appendChild(selectInput);
    const poster = document.createElement("img");
    poster.className = "candidate-poster";
    if (item.poster_url) {
      poster.src = item.poster_url;
      poster.alt = `${item.title} poster`;
      poster.loading = "lazy";
    } else {
      poster.alt = "";
    }
    const meta = document.createElement("div");
    meta.className = "candidate-meta history-meta";
    const title = document.createElement("h3");
    title.textContent = item.title;
    const detail = document.createElement("p");
    const watchedAt = new Date(item.watched_at);
    const watchedLabel = Number.isNaN(watchedAt.valueOf())
      ? ""
      : watchedAt.toLocaleString();
    const year = item.year ? item.year : "Year unknown";
    const mediaType = formatMediaType(item.media_type);
    const detailParts = [year, mediaType];
    if (item.media_type === "tv") {
      const episodeLabel = formatSeasonEpisode(item.season_number, item.episode_number);
      if (episodeLabel) {
        detailParts.push(
          item.episode_title ? `${episodeLabel} · ${item.episode_title}` : episodeLabel,
        );
      } else {
        detailParts.push("Episode unknown");
      }
    }
    if (watchedLabel) {
      detailParts.push(`Watched ${watchedLabel}`);
    }
    if (item.rating !== null && item.rating !== undefined) {
      const ratingLabel = formatRating(item.rating);
      if (ratingLabel) {
        detailParts.push(`Rated ${ratingLabel}`);
      }
    }
    const letterboxdStatus = item.letterboxd_status;
    const letterboxdSucceeded = letterboxdStatus === "succeeded";
    if (letterboxdStatus) {
      const statusLabel = letterboxdStatus.replace(/_/g, " ");
      detailParts.push(`Letterboxd ${statusLabel}`);
    }
    if (!letterboxdSucceeded && item.letterboxd_last_error) {
      const rawError = String(item.letterboxd_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`Letterboxd error: ${shortError}`);
    }
    const traktStatus = item.trakt_status;
    const traktSucceeded = traktStatus === "succeeded";
    if (traktStatus) {
      const statusLabel = traktStatus.replace(/_/g, " ");
      detailParts.push(`Trakt ${statusLabel}`);
    }
    if (!traktSucceeded && item.trakt_last_error) {
      const rawError = String(item.trakt_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`Trakt error: ${shortError}`);
    }
    const simklStatus = item.simkl_status;
    const simklSucceeded = simklStatus === "succeeded";
    if (simklStatus) {
      const statusLabel = simklStatus.replace(/_/g, " ");
      detailParts.push(`SIMKL ${statusLabel}`);
    }
    if (!simklSucceeded && item.simkl_last_error) {
      const rawError = String(item.simkl_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`SIMKL error: ${shortError}`);
    }
    const stremioStatus = item.stremio_status;
    const stremioSucceeded = stremioStatus === "succeeded";
    if (stremioStatus) {
      const statusLabel = stremioStatus.replace(/_/g, " ");
      detailParts.push(`Stremio ${statusLabel}`);
    }
    if (!stremioSucceeded && item.stremio_last_error) {
      const rawError = String(item.stremio_last_error);
      const shortError =
        rawError.length > 120 ? `${rawError.slice(0, 120)}...` : rawError;
      detailParts.push(`Stremio error: ${shortError}`);
    }
    detail.textContent = detailParts.join(" · ");

    const header = document.createElement("div");
    header.className = "history-header";

    const actions = document.createElement("div");
    actions.className = "history-actions";

    const menuButton = document.createElement("button");
    menuButton.type = "button";
    menuButton.className = "menu-button";
    menuButton.setAttribute("aria-haspopup", "true");
    menuButton.setAttribute("aria-expanded", "false");
    menuButton.setAttribute("aria-label", "More actions");
    menuButton.setAttribute("data-menu-button", "true");

    const dotStack = document.createElement("span");
    dotStack.className = "menu-dots";
    for (let i = 0; i < 3; i += 1) {
      const dot = document.createElement("span");
      dot.className = "menu-dot";
      dotStack.appendChild(dot);
    }
    menuButton.appendChild(dotStack);

    const menuPanel = document.createElement("div");
    menuPanel.className = "menu-panel";
    menuPanel.setAttribute("role", "menu");
    menuPanel.setAttribute("data-menu-panel", "true");

    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.textContent = "Edit watch";
    editButton.setAttribute("role", "menuitem");

    const metadataButton = document.createElement("button");
    metadataButton.type = "button";
    metadataButton.textContent = "View metadata";
    metadataButton.setAttribute("role", "menuitem");

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "danger-button";
    deleteButton.textContent = "Delete";
    deleteButton.setAttribute("role", "menuitem");

    menuPanel.appendChild(editButton);
    menuPanel.appendChild(metadataButton);
    menuPanel.appendChild(deleteButton);

    actions.appendChild(menuButton);
    actions.appendChild(menuPanel);

    const editRow = document.createElement("div");
    editRow.className = "history-edit";

    const editInput = document.createElement("input");
    editInput.type = "datetime-local";
    editInput.value = formatDateTimeInput(item.watched_at);

    const ratingSelect = buildRatingSelect(item.rating);
    const initialRatingValue =
      item.rating !== null && item.rating !== undefined ? String(item.rating) : "";

    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.textContent = "Save";

    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "secondary-button";
    cancelButton.textContent = "Cancel";

    editRow.appendChild(editInput);
    editRow.appendChild(ratingSelect);
    editRow.appendChild(saveButton);
    editRow.appendChild(cancelButton);

    menuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const isOpen = menuPanel.classList.contains("is-open");
      closeHistoryMenus();
      if (!isOpen) {
        menuPanel.classList.add("is-open");
        menuButton.setAttribute("aria-expanded", "true");
      }
    });

    editButton.addEventListener("click", () => {
      closeHistoryMenus();
      editRow.classList.add("is-visible");
      editInput.focus();
    });

    metadataButton.addEventListener("click", () => {
      closeHistoryMenus();
      openMetadataModal(item);
    });

    cancelButton.addEventListener("click", () => {
      editInput.value = formatDateTimeInput(item.watched_at);
      ratingSelect.value = initialRatingValue;
      editRow.classList.remove("is-visible");
    });

    saveButton.addEventListener("click", async () => {
      setMessage("history-message", "");
      const payload = {};
      const rawValue = editInput.value;
      const initialWatchValue = formatDateTimeInput(item.watched_at);
      if (rawValue !== initialWatchValue) {
        const watchedAt = parseDateTimeInput(rawValue);
        if (rawValue && !watchedAt) {
          setMessage("history-message", "Enter a valid watch date/time.", true);
          return;
        }
        payload.watched_at = watchedAt;
      }
      if (ratingSelect.value !== initialRatingValue) {
        payload.rating = ratingSelect.value
          ? Number(ratingSelect.value)
          : null;
      }
      if (!Object.keys(payload).length) {
        setMessage("history-message", "No changes to save.", true);
        return;
      }
      try {
        setMessage("history-message", "Saving...");
        await requestJSON(`/api/history/items/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        setMessage("history-message", "Update saved.");
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });

    deleteButton.addEventListener("click", async () => {
      closeHistoryMenus();
      const confirmed = window.confirm(
        `Delete "${item.title}" from your history?`
      );
      if (!confirmed) {
        return;
      }
      const deleteIntegrations = window.confirm(
        "Also delete this item from all connected integrations? " +
          "Click OK to remove it there too, or Cancel to delete locally only."
      );
      try {
        setMessage("history-message", "Deleting...");
        const url = deleteIntegrations
          ? `/api/history/items/${item.id}?delete_integrations=true`
          : `/api/history/items/${item.id}`;
        await requestJSON(url, {
          method: "DELETE",
        });
        setMessage("history-message", "Entry deleted.");
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });

    header.appendChild(title);
    header.appendChild(actions);
    meta.appendChild(header);
    meta.appendChild(detail);
    meta.appendChild(editRow);
    card.appendChild(selectWrap);
    card.appendChild(poster);
    card.appendChild(meta);
    container.appendChild(card);
  });
}

function bindHistoryClear() {
  const clearButton = document.getElementById("history-clear");
  if (!clearButton) {
    return;
  }
  clearButton.addEventListener("click", async () => {
    const confirmed = window.confirm(
      "Clear your entire watch history? This cannot be undone."
    );
    if (!confirmed) {
      return;
    }
    try {
      setMessage("history-message", "Clearing history...");
      await requestJSON("/api/history/items", { method: "DELETE" });
      setMessage("history-message", "History cleared.");
      await loadHistory();
    } catch (error) {
      setMessage("history-message", error.message, true);
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  const body = document.body;
  const requiresAuth = body && body.dataset.requiresAuth === "true";
  const guestOnly = body && body.dataset.guestOnly === "true";
  const user = await loadCurrentUser();
  applyAuthVisibility(user);

  if (requiresAuth && !user) {
    window.location.href = "/static/login.html";
    return;
  }

  if (guestOnly && user) {
    window.location.href = "/";
    return;
  }

  bindForm("login-form", handleLogin);
  bindForm("register-form", handleRegister);
  bindForm("letterboxd-form", handleLetterboxdSave);
  bindForm("letterboxd-import-form", (data) =>
    handleImportScheduleSave("letterboxd", data)
  );
  bindForm("stremio-form", handleStremioConnect);
  bindForm("stremio-import-form", (data) =>
    handleImportScheduleSave("stremio", data)
  );
  bindForm("trakt-import-form", (data) =>
    handleImportScheduleSave("trakt", data)
  );
  bindForm("simkl-import-form", (data) =>
    handleImportScheduleSave("simkl", data)
  );
  bindForm("settings-form", handleSettingsSave);
  bindForm("tmdb-form", handleTmdbSave);
  bindForm("tvdb-form", handleTvdbSave);
  bindForm("tvmaze-form", handleTvmazeSave);
  bindForm("imdb-form", handleImdbSave);
  bindHistoryClear();
  bindForm("kitsu-form", handleKitsuSave);
  bindForm("myanimelist-form", handleMyAnimeListSave);
  bindForm("lookup-form", handleLookupSubmit);
  bindForm("confirm-form", handleLookupConfirm);

  const candidateList = document.getElementById("candidate-list");
  if (candidateList) {
    candidateList.addEventListener("change", (event) => {
      if (event.target && event.target.name === "candidate_id") {
        handleCandidateSelection();
      }
    });
  }
  const seasonSelect = document.getElementById("season-select");
  if (seasonSelect) {
    seasonSelect.addEventListener("change", () => {
      const seasonNumber = Number(seasonSelect.value);
      if (Number.isNaN(seasonNumber) || !episodeState.tmdbId) {
        return;
      }
      loadEpisodes(episodeState.tmdbId, seasonNumber);
    });
  }

  document.querySelectorAll("[data-logout]").forEach((button) => {
    button.addEventListener("click", handleLogout);
  });

  const letterboxdTest = document.getElementById("letterboxd-test");
  if (letterboxdTest) {
    letterboxdTest.addEventListener("click", handleLetterboxdTest);
  }
  const importAllButton = document.getElementById("import-all-button");
  if (importAllButton) {
    importAllButton.addEventListener("click", handleImportAll);
  }
  const letterboxdImportNow = document.getElementById("letterboxd-import-now");
  if (letterboxdImportNow) {
    letterboxdImportNow.addEventListener("click", () =>
      handleImportNow("letterboxd")
    );
  }
  const letterboxdParse = document.getElementById("letterboxd-parse");
  if (letterboxdParse) {
    letterboxdParse.addEventListener("click", handleLetterboxdParse);
  }
  const letterboxdDisconnect = document.getElementById("letterboxd-disconnect");
  if (letterboxdDisconnect) {
    letterboxdDisconnect.addEventListener("click", handleLetterboxdDisconnect);
  }
  const traktConnect = document.getElementById("trakt-connect");
  if (traktConnect) {
    traktConnect.addEventListener("click", handleTraktConnect);
  }
  const traktDisconnect = document.getElementById("trakt-disconnect");
  if (traktDisconnect) {
    traktDisconnect.addEventListener("click", handleTraktDisconnect);
  }
  const traktImportNow = document.getElementById("trakt-import-now");
  if (traktImportNow) {
    traktImportNow.addEventListener("click", () =>
      handleImportNow("trakt")
    );
  }
  const simklImportNow = document.getElementById("simkl-import-now");
  if (simklImportNow) {
    simklImportNow.addEventListener("click", () =>
      handleImportNow("simkl")
    );
  }
  const stremioImportNow = document.getElementById("stremio-import-now");
  if (stremioImportNow) {
    stremioImportNow.addEventListener("click", () =>
      handleImportNow("stremio")
    );
  }

  const simklConnect = document.getElementById("simkl-connect");
  if (simklConnect) {
    simklConnect.addEventListener("click", handleSimklConnect);
  }
  const simklDisconnect = document.getElementById("simkl-disconnect");
  if (simklDisconnect) {
    simklDisconnect.addEventListener("click", handleSimklDisconnect);
  }
  const stremioDisconnect = document.getElementById("stremio-disconnect");
  if (stremioDisconnect) {
    stremioDisconnect.addEventListener("click", handleStremioDisconnect);
  }

  if (user) {
    try {
      await Promise.all([
        loadIntegrations(),
        loadSettings(),
        loadMetadataProviders(),
        loadHistory(),
      ]);
    } catch (error) {
      console.error("initial load failed", error);
    }
  }
});
