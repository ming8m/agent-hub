(function installTaskStatus(root, createStatus) {
  const api = createStatus();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AgentHubTaskStatus = api;
})(globalThis, function createTaskStatus() {
  const TERMINAL = new Set(['succeeded', 'failed', 'execution_timeout', 'cancelled', 'interrupted']);
  const LABELS = {
    queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败',
    execution_timeout: '执行超时', cancelled: '已取消', interrupted: '服务重启中断'
  };
  function isTerminal(status) { return TERMINAL.has(status); }
  function label(task) {
    const status = task?.status || '';
    if (status === 'interrupted') return LABELS.interrupted;
    if (task?.overdue && !isTerminal(status)) return '已逾期 · 执行仍在继续';
    return LABELS[status] || status || '状态未知';
  }
  return Object.freeze({ isTerminal, label });
});
