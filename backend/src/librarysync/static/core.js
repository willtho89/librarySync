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
  const toggleButton = document.querySelector("[data-mobile-menu-toggle]");
  const closeButton = document.querySelector("[data-mobile-menu-close]");
  const backdrop = document.querySelector("[data-mobile-menu-backdrop]");
  const panel = document.querySelector("[data-mobile-menu-panel]");

  if (!toggleButton || !panel || !backdrop) {
    return;
  }

  if (!closeButton) {
    console.warn("Mobile menu close button not found");
  }

  function openMenu() {
    panel.classList.add("is-open");
    backdrop.classList.add("is-open");
    toggleButton.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
  }

  function closeMenu() {
    panel.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    toggleButton.setAttribute("aria-expanded", "false");
    document.body.style.overflow = "";
  }

  toggleButton.addEventListener("click", () => {
    const isOpen = panel.classList.contains("is-open");
    if (isOpen) {
      closeMenu();
    } else {
      openMenu();
    }
  });

  if (closeButton) {
    closeButton.addEventListener("click", closeMenu);
  }

  backdrop.addEventListener("click", closeMenu);

  panel.querySelectorAll("a").forEach((link) => {
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

async function handleLogout() {
  try {
    await requestJSON("/api/auth/logout", { method: "POST" });
  } catch (error) {
    console.error("logout failed", error);
  }
  window.location.href = "/login";
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    return;
  }
  navigator.serviceWorker.register("/static/service-worker.js").catch((error) => {
    console.warn("service worker registration failed", error);
  });
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

document.addEventListener("DOMContentLoaded", () => {
  initBase().catch((error) => {
    console.error("page bootstrap failed", error);
  });
});
