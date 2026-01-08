function formatLabel(value) {
  if (!value) {
    return "";
  }
  return String(value)
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatProvider(value) {
  if (!value) {
    return "Unknown";
  }
  const normalized = String(value).toLowerCase();
  const overrides = {
    simkl: "SIMKL",
    trakt: "Trakt",
    stremio: "Stremio",
    letterboxd: "Letterboxd",
    aiostreams: "AIOStreams",
    manual: "Manual",
    internal: "Internal",
  };
  return overrides[normalized] || formatLabel(normalized);
}

function formatInterval(seconds) {
  if (!seconds) {
    return "Manual";
  }
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) {
    return "Manual";
  }
  if (value < 60) {
    return `Every ${Math.round(value)}s`;
  }
  if (value < 3600) {
    return `Every ${Math.round(value / 60)}m`;
  }
  if (value < 86400) {
    return `Every ${Math.round(value / 3600)}h`;
  }
  return `Every ${Math.round(value / 86400)}d`;
}

function formatRelativeTime(value) {
  if (!value) {
    return "Not scheduled";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "Unknown";
  }
  const diffSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absSeconds = Math.abs(diffSeconds);
  if (absSeconds < 30) {
    return "now";
  }
  let unit = "sec";
  let amount = absSeconds;
  if (absSeconds >= 86400) {
    unit = "day";
    amount = Math.round(absSeconds / 86400);
  } else if (absSeconds >= 3600) {
    unit = "hr";
    amount = Math.round(absSeconds / 3600);
  } else if (absSeconds >= 60) {
    unit = "min";
    amount = Math.round(absSeconds / 60);
  }
  const label = amount === 1 ? unit : `${unit}s`;
  return diffSeconds >= 0 ? `in ${amount} ${label}` : `${amount} ${label} ago`;
}

function statusBadgeClass(status) {
  if (!status) {
    return "status-unknown";
  }
  const normalized = String(status);
  if (normalized === "blacklisted") {
    return "status-unknown";
  }
  if (normalized === "in_progress") {
    return "status-active";
  }
  if (normalized === "succeeded" || normalized.startsWith("synced_")) {
    return "status-success";
  }
  if (normalized.includes("fail")) {
    return "status-failed";
  }
  return "status-pending";
}
