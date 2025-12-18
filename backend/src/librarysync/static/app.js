const authState = {
  user: null,
  loaded: false,
  promise: null,
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

async function loadSettings() {
  const form = document.getElementById("settings-form");
  if (!form) {
    return;
  }
  const data = await requestJSON("/api/settings");
  const pollInput = form.querySelector("input[name='poll_interval']");
  const thresholdInput = form.querySelector("input[name='completion_threshold']");
  if (pollInput && typeof data.poll_interval === "number") {
    pollInput.value = data.poll_interval.toString();
  }
  if (thresholdInput && typeof data.completion_threshold === "number") {
    thresholdInput.value = data.completion_threshold.toString();
  }
}

async function handleSettingsSave(data) {
  setMessage("settings-message", "");
  const pollValue = (data.get("poll_interval") || "").trim();
  const thresholdValue = (data.get("completion_threshold") || "").trim();
  const payload = {
    poll_interval: pollValue ? Number(pollValue) : null,
    completion_threshold: thresholdValue ? Number(thresholdValue) : null,
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
  bindForm("settings-form", handleSettingsSave);

  document.querySelectorAll("[data-logout]").forEach((button) => {
    button.addEventListener("click", handleLogout);
  });

  if (user) {
    try {
      await Promise.all([loadIntegrations(), loadSettings()]);
    } catch (error) {
      console.error("initial load failed", error);
    }
  }
});
