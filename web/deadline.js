(function installDeadline(root, createDeadline) {
  const api = createDeadline();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AgentHubDeadline = api;
})(globalThis, function createDeadline() {
  function formatLocalInput(nowMs, seconds) {
    if (!Number.isFinite(seconds) || seconds < 1) return '';
    const target = new Date(Math.ceil((nowMs + seconds * 1000) / 1000) * 1000);
    const local = new Date(target.getTime() - target.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 19);
  }
  function relativeSeconds(value, nowMs) {
    const targetMs = new Date(value).getTime();
    if (!Number.isFinite(targetMs) || !Number.isFinite(nowMs)) return null;
    return Math.ceil((targetMs - nowMs) / 1000);
  }
  return Object.freeze({ formatLocalInput, relativeSeconds });
});
