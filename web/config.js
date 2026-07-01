(() => {
  const cloudApiBase = "https://api-production-4f23.up.railway.app";
  const localHosts = new Set(["127.0.0.1", "localhost", "::1"]);
  const isLocalApp = localHosts.has(window.location.hostname);
  window.JOB_AGENT_API_BASE = window.JOB_AGENT_API_BASE || (isLocalApp ? "" : cloudApiBase);
})();
