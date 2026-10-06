const authState = {
  user: null,
  loaded: false,
  promise: null,
};
const themeState = {
  mode: "system",
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

function getStoredTheme() {
  try {
    return localStorage.getItem("librarysync_theme");
  } catch (error) {
    return null;
  }
}

function setStoredTheme(value) {
  try {
    localStorage.setItem("librarysync_theme", value);
  } catch (error) {
    // ignore storage errors
  }
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (!root) {
    return;
  }
  if (mode === "light" || mode === "dark") {
    root.dataset.theme = mode;
  } else {
    delete root.dataset.theme;
  }
  themeState.mode = mode;
  document.querySelectorAll("[data-theme-option]").forEach((option) => {
    option.checked = option.value === mode;
  });
  document.querySelectorAll("[data-theme-label]").forEach((label) => {
    label.textContent = mode === "system" ? "System" : mode === "dark" ? "Dark" : "Light";
  });
}

function initThemeToggle() {
  const stored = getStoredTheme();
  const initial =
    stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  applyTheme(initial);
  document.querySelectorAll("[data-theme-option]").forEach((option) => {
    option.addEventListener("change", () => {
      if (!option.checked) {
        return;
      }
      const value = option.value;
      setStoredTheme(value);
      applyTheme(value);
    });
  });
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const next =
        themeState.mode === "system"
          ? "light"
          : themeState.mode === "light"
            ? "dark"
            : "system";
      setStoredTheme(next);
      applyTheme(next);
    });
  });
}

function initMobileMenu() {
  const toggleButtons = Array.from(document.querySelectorAll("[data-mobile-menu-toggle]"));
  const closeButton = document.querySelector("[data-mobile-menu-close]");
  const backdrop = document.querySelector("[data-mobile-menu-backdrop]");
  const panel = document.querySelector("[data-mobile-menu-panel]");

  if (!toggleButtons.length || !panel || !backdrop) {
    return;
  }

  if (!closeButton) {
    console.warn("Mobile menu close button not found");
  }

  const setExpanded = (value) => {
    toggleButtons.forEach((button) => {
      button.setAttribute("aria-expanded", value ? "true" : "false");
    });
  };

  function openMenu() {
    panel.classList.add("is-open");
    backdrop.classList.add("is-open");
    panel.setAttribute("aria-hidden", "false");
    setExpanded(true);
    document.body.style.overflow = "hidden";
  }

  function closeMenu() {
    panel.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    panel.setAttribute("aria-hidden", "true");
    setExpanded(false);
    document.body.style.overflow = "";
  }

  toggleButtons.forEach((button) => {
    if (panel.id && !button.hasAttribute("aria-controls")) {
      button.setAttribute("aria-controls", panel.id);
    }
    button.addEventListener("click", () => {
      const isOpen = panel.classList.contains("is-open");
      if (isOpen) {
        closeMenu();
      } else {
        openMenu();
      }
    });
  });

  if (closeButton) {
    closeButton.addEventListener("click", closeMenu);
  }

  backdrop.addEventListener("click", closeMenu);

  panel.querySelectorAll("a, button[data-logout]").forEach((link) => {
    link.addEventListener("click", closeMenu);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && panel.classList.contains("is-open")) {
      closeMenu();
    }
  });
}

function initTabsets() {
  document.querySelectorAll("[data-tabset]").forEach((tabset) => {
    const buttons = Array.from(tabset.querySelectorAll("[data-tab-button]"));
    const panels = Array.from(tabset.querySelectorAll("[data-tab-panel]"));
    if (!buttons.length || !panels.length) {
      return;
    }

    const tabs = buttons.map((button) => button.dataset.tab).filter(Boolean);
    const buttonMap = new Map();
    buttons.forEach((button) => {
      if (button.dataset.tab) {
        buttonMap.set(button.dataset.tab, button);
      }
    });

    function setActive(tabId, options = {}) {
      const target = buttonMap.get(tabId);
      if (!target) {
        return;
      }
      buttons.forEach((button) => {
        const isActive = button.dataset.tab === tabId;
        button.setAttribute("aria-selected", isActive ? "true" : "false");
        button.tabIndex = isActive ? 0 : -1;
      });
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.tab !== tabId;
      });
      if (options.updateHash) {
        history.replaceState(null, "", `#${tabId}`);
      }
      if (options.focus) {
        target.focus();
      }
    }

    function tabFromHash() {
      const raw = window.location.hash.replace("#", "");
      if (!raw) {
        return null;
      }
      return buttonMap.has(raw) ? raw : null;
    }

    setActive(tabFromHash() || tabs[0], { updateHash: false });

    buttons.forEach((button, index) => {
      button.addEventListener("click", () => {
        setActive(button.dataset.tab, { updateHash: true });
      });
      button.addEventListener("keydown", (event) => {
        if (event.key === "Home") {
          event.preventDefault();
          setActive(tabs[0], { updateHash: true, focus: true });
          return;
        }
        if (event.key === "End") {
          event.preventDefault();
          setActive(tabs[tabs.length - 1], { updateHash: true, focus: true });
          return;
        }
        const isNext = event.key === "ArrowRight" || event.key === "ArrowDown";
        const isPrev = event.key === "ArrowLeft" || event.key === "ArrowUp";
        if (!isNext && !isPrev) {
          return;
        }
        event.preventDefault();
        const direction = isNext ? 1 : -1;
        const nextIndex = (index + direction + tabs.length) % tabs.length;
        setActive(tabs[nextIndex], { updateHash: true, focus: true });
      });
    });

    window.addEventListener("hashchange", () => {
      const hashTab = tabFromHash();
      if (hashTab) {
        setActive(hashTab, { updateHash: false });
      }
    });
  });
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
  "[contenteditable='true']",
].join(",");

function getFocusableElements(container) {
  if (!container) {
    return [];
  }
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.closest("[hidden], [inert]") && el.getClientRects().length > 0
  );
}

// Keeps keyboard focus inside `container` until the returned release function is called.
// Focus moves into the container on activation and returns to the previously focused
// element on release. `onEscape` is called when Escape is pressed while trapped.
function activateFocusTrap(container, options = {}) {
  if (!container) {
    return () => {};
  }
  const { initialFocus = null, onEscape = null } = options;
  const previouslyFocused =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;

  const focusContainer = () => {
    if (!container.hasAttribute("tabindex")) {
      container.setAttribute("tabindex", "-1");
    }
    container.focus();
  };

  const handleKeydown = (event) => {
    if (event.key === "Escape" && typeof onEscape === "function") {
      event.preventDefault();
      onEscape(event);
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const focusable = getFocusableElements(container);
    if (!focusable.length) {
      event.preventDefault();
      focusContainer();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    const outside = !container.contains(active);
    if (event.shiftKey && (active === first || active === container || outside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || outside)) {
      event.preventDefault();
      first.focus();
    }
  };

  const handleFocusIn = (event) => {
    if (!container.contains(event.target)) {
      const focusable = getFocusableElements(container);
      if (focusable.length) {
        focusable[0].focus();
      } else {
        focusContainer();
      }
    }
  };

  document.addEventListener("keydown", handleKeydown, true);
  document.addEventListener("focusin", handleFocusIn);

  const target =
    (typeof initialFocus === "string"
      ? container.querySelector(initialFocus)
      : initialFocus) || getFocusableElements(container)[0];
  if (target) {
    target.focus();
  } else {
    focusContainer();
  }

  let released = false;
  return (releaseOptions = {}) => {
    if (released) {
      return;
    }
    released = true;
    document.removeEventListener("keydown", handleKeydown, true);
    document.removeEventListener("focusin", handleFocusIn);
    const { restoreFocus = true } = releaseOptions;
    if (restoreFocus && previouslyFocused && previouslyFocused.isConnected) {
      previouslyFocused.focus();
    }
  };
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

function createAbortError() {
  try {
    return new DOMException("The operation was aborted.", "AbortError");
  } catch (error) {
    const abortError = new Error("The operation was aborted.");
    abortError.name = "AbortError";
    return abortError;
  }
}

function isAbortError(error) {
  return Boolean(error && error.name === "AbortError");
}

function waitWithSignal(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) {
      reject(createAbortError());
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(createAbortError());
    };
    const timer = window.setTimeout(() => {
      if (signal) {
        signal.removeEventListener("abort", onAbort);
      }
      resolve();
    }, ms);
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
    }
  });
}

const METADATA_LOOKUP_TIMEOUT_MESSAGE =
  "Lookup timed out — is the metadata worker running?";

// Polls an async metadata lookup until it completes, fails, times out or is aborted.
// Resolves with the completed lookup payload. Rejects with an Error whose `code` is
// "failed" or "timeout", with request errors from requestJSON, or with an AbortError
// when `signal` aborts. `onUpdate` receives each still-pending payload (partial results).
async function pollMetadataLookup(lookupId, options = {}) {
  const {
    timeoutMs = 60000,
    initialDelayMs = 1200,
    maxDelayMs = 5000,
    backoffFactor = 1.5,
    onUpdate = null,
    signal = null,
  } = options;
  const deadline = Date.now() + timeoutMs;
  let delayMs = initialDelayMs;
  while (true) {
    if (signal && signal.aborted) {
      throw createAbortError();
    }
    const data = await requestJSON(`/api/metadata/lookup/${encodeURIComponent(lookupId)}`, {
      signal: signal || undefined,
    });
    if (signal && signal.aborted) {
      throw createAbortError();
    }
    const status = data && data.status;
    if (status === "completed") {
      return data;
    }
    if (status === "failed") {
      const failure = new Error((data && data.error) || "Lookup failed.");
      failure.code = "failed";
      failure.lookup = data;
      throw failure;
    }
    if (typeof onUpdate === "function") {
      onUpdate(data);
    }
    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) {
      const timeout = new Error(METADATA_LOOKUP_TIMEOUT_MESSAGE);
      timeout.code = "timeout";
      timeout.lookup = data;
      throw timeout;
    }
    await waitWithSignal(Math.min(delayMs, remainingMs), signal);
    delayMs = Math.min(maxDelayMs, Math.round(delayMs * backoffFactor));
  }
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
  if (document.body) {
    document.body.dataset.authState = user ? "auth" : "guest";
  }
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

async function clearOfflineCaches() {
  // Cached pages can contain user data, so drop them whenever the session ends.
  if (!("caches" in window)) {
    return;
  }
  try {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((key) => key.startsWith("librarysync")).map((key) => caches.delete(key))
    );
  } catch (error) {
    console.warn("failed to clear offline caches", error);
  }
}

async function handleLogout() {
  try {
    await requestJSON("/api/auth/logout", { method: "POST" });
  } catch (error) {
    console.error("logout failed", error);
  }
  await clearOfflineCaches();
  window.location.href = "/login";
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    return;
  }
  const version = document.documentElement.dataset.appVersion || "";
  const scriptUrl = version
    ? `/service-worker.js?v=${encodeURIComponent(version)}`
    : "/service-worker.js";
  navigator.serviceWorker.register(scriptUrl, { scope: "/" }).catch((error) => {
    console.warn("service worker registration failed", error);
  });
  // Older releases registered the worker under /static/, where it could never control pages.
  if (typeof navigator.serviceWorker.getRegistrations === "function") {
    navigator.serviceWorker
      .getRegistrations()
      .then((registrations) => {
        registrations.forEach((registration) => {
          const worker =
            registration.active || registration.waiting || registration.installing;
          if (worker && new URL(worker.scriptURL).pathname === "/static/service-worker.js") {
            registration.unregister();
          }
        });
      })
      .catch(() => {});
  }
}

async function initBase() {
  const body = document.body;
  const requiresAuth = body && body.dataset.requiresAuth === "true";
  const guestOnly = body && body.dataset.guestOnly === "true";

  initThemeToggle();
  initMobileMenu();
  initTabsets();

  const user = await loadCurrentUser();
  applyAuthVisibility(user);

  if (requiresAuth && !user) {
    window.location.href = "/login";
    return;
  }

  if (guestOnly && user) {
    window.location.href = "/";
    return;
  }

  document.querySelectorAll("[data-logout]").forEach((button) => {
    button.addEventListener("click", handleLogout);
  });

  if (typeof window.librarysyncPageInit === "function") {
    await window.librarysyncPageInit({ user });
  }

  registerServiceWorker();
}

function showToast(message, isError = false, duration = 3000) {
  const container = document.getElementById("toast-container");
  if (!container) {
    console.warn("Toast container not found");
    return;
  }

  // Build the toast from DOM nodes: messages can contain server-provided text,
  // so they must never be parsed as HTML.
  const toast = document.createElement("div");
  toast.className = `toast ${isError ? "toast-error" : "toast-success"}`;

  const content = document.createElement("div");
  content.className = "toast-content";
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = isError ? "⚠️" : "✓";
  const text = document.createElement("span");
  text.className = "toast-message";
  text.textContent = message === null || message === undefined ? "" : String(message);
  content.appendChild(icon);
  content.appendChild(text);

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "toast-close";
  closeButton.setAttribute("aria-label", "Close notification");
  closeButton.textContent = "×";

  toast.appendChild(content);
  toast.appendChild(closeButton);

  // Add close button functionality
  closeButton.addEventListener("click", () => {
    toast.remove();
  });

  // Auto-hide after duration
  if (duration > 0) {
    setTimeout(() => {
      if (toast.parentNode) {
        toast.remove();
      }
    }, duration);
  }

  container.appendChild(toast);

  // Trigger animation
  requestAnimationFrame(() => {
    toast.classList.add("toast-visible");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initBase().catch((error) => {
    console.error("page bootstrap failed", error);
  });
});
