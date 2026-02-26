function isIntegrationConnected(integration) {
  return (
    !!integration &&
    (integration.has_secrets || integration.status === "connected")
  );
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
  if (normalized === "aiostreams") {
    return "AIOStreams Proxy";
  }
  if (normalized === "anilist") {
    return "AniList";
  }
  if (normalized === "publicmetadb") {
    return "PublicMetaDB";
  }
  return normalized.toUpperCase();
}
