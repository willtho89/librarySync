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
  const form = document.getElementById("aiostreams-form");
  if (!form) {
    return;
  }
  const data = await requestJSON("/api/integrations");
  const integrations = data && data.integrations ? data.integrations : [];
  const aiostreams = integrations.find((item) => item.provider === "aiostreams");
  const baseInput = form.querySelector("input[name='base_url']");
  if (aiostreams && aiostreams.config && aiostreams.config.base_url && baseInput) {
    baseInput.value = aiostreams.config.base_url;
  }
  const messageEl = document.getElementById("aiostreams-message");
  if (aiostreams && aiostreams.has_secrets && messageEl && !messageEl.textContent) {
    setMessage("aiostreams-message", "API key is stored securely.");
  }

  const letterboxdForm = document.getElementById("letterboxd-form");
  if (!letterboxdForm) {
    return;
  }
  const letterboxd = integrations.find((item) => item.provider === "letterboxd");
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
  if (
    letterboxd &&
    letterboxd.has_secrets &&
    letterboxdMessage &&
    !letterboxdMessage.textContent
  ) {
    setMessage("letterboxd-message", "Credentials are stored securely.");
  }

  const trakt = integrations.find((item) => item.provider === "trakt");
  const traktMessage = document.getElementById("trakt-message");
  const traktConnect = document.getElementById("trakt-connect");
  const traktDisconnect = document.getElementById("trakt-disconnect");
  if (trakt && trakt.has_secrets) {
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
}

async function handleAIOStreamsSave(data, form) {
  setMessage("aiostreams-message", "");
  const baseUrl = (data.get("base_url") || "").trim();
  const apiKey = (data.get("api_key") || "").trim();
  if (!baseUrl) {
    setMessage("aiostreams-message", "Base URL is required.", true);
    return;
  }
  const payload = { base_url: baseUrl };
  if (apiKey) {
    payload.api_key = apiKey;
  }
  try {
    await requestJSON("/api/integrations/aiostreams", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const apiKeyInput = form.querySelector("input[name='api_key']");
    if (apiKeyInput) {
      apiKeyInput.value = "";
    }
    setMessage("aiostreams-message", "Saved.");
    await loadIntegrations();
  } catch (error) {
    setMessage("aiostreams-message", error.message, true);
  }
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

async function loadSettings() {
  const form = document.getElementById("settings-form");
  if (!form) {
    return;
  }
  const data = await requestJSON("/api/settings");
  const pollInput = form.querySelector("input[name='poll_interval']");
  const thresholdInput = form.querySelector("input[name='completion_threshold']");
  const includeAdultInput = form.querySelector(
    "input[name='include_adult_in_search']"
  );
  if (pollInput && typeof data.poll_interval === "number") {
    pollInput.value = data.poll_interval.toString();
  }
  if (thresholdInput && typeof data.completion_threshold === "number") {
    thresholdInput.value = data.completion_threshold.toString();
  }
  if (includeAdultInput) {
    includeAdultInput.checked = !!data.include_adult_in_search;
  }
}

async function handleSettingsSave(data) {
  setMessage("settings-message", "");
  const pollValue = (data.get("poll_interval") || "").trim();
  const thresholdValue = (data.get("completion_threshold") || "").trim();
  const includeAdult = data.get("include_adult_in_search") === "on";
  const payload = {
    poll_interval: pollValue ? Number(pollValue) : null,
    completion_threshold: thresholdValue ? Number(thresholdValue) : null,
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

async function loadHistory() {
  const container = document.getElementById("history-list");
  if (!container) {
    return;
  }
  const data = await requestJSON("/api/history/items");
  const items = data && data.items ? data.items : [];
  if (!items.length) {
    container.textContent = "No watched titles yet.";
    return;
  }
  container.innerHTML = "";
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "history-card";
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
    detail.textContent = detailParts.join(" · ");

    const actions = document.createElement("div");
    actions.className = "history-actions";

    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.className = "secondary-button";
    editButton.textContent = "Edit";

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "danger-button";
    deleteButton.textContent = "Delete";

    actions.appendChild(editButton);
    actions.appendChild(deleteButton);

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

    editButton.addEventListener("click", () => {
      editRow.classList.add("is-visible");
      editInput.focus();
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
      const confirmed = window.confirm(
        `Delete "${item.title}" from your history?`
      );
      if (!confirmed) {
        return;
      }
      try {
        setMessage("history-message", "Deleting...");
        await requestJSON(`/api/history/items/${item.id}`, {
          method: "DELETE",
        });
        setMessage("history-message", "Entry deleted.");
        await loadHistory();
      } catch (error) {
        setMessage("history-message", error.message, true);
      }
    });

    meta.appendChild(title);
    meta.appendChild(detail);
    meta.appendChild(actions);
    meta.appendChild(editRow);
    card.appendChild(poster);
    card.appendChild(meta);
    container.appendChild(card);
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
  bindForm("aiostreams-form", handleAIOStreamsSave);
  bindForm("letterboxd-form", handleLetterboxdSave);
  bindForm("settings-form", handleSettingsSave);
  bindForm("tmdb-form", handleTmdbSave);
  bindForm("tvdb-form", handleTvdbSave);
  bindForm("tvmaze-form", handleTvmazeSave);
  bindForm("imdb-form", handleImdbSave);
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
  const letterboxdParse = document.getElementById("letterboxd-parse");
  if (letterboxdParse) {
    letterboxdParse.addEventListener("click", handleLetterboxdParse);
  }
  const traktConnect = document.getElementById("trakt-connect");
  if (traktConnect) {
    traktConnect.addEventListener("click", handleTraktConnect);
  }
  const traktDisconnect = document.getElementById("trakt-disconnect");
  if (traktDisconnect) {
    traktDisconnect.addEventListener("click", handleTraktDisconnect);
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
