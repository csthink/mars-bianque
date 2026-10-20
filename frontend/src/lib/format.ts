// Display formats shared by the pages.

/** 980 → "980"; 38210 → "38.2k". */
export function formatTokens(tokens: number): string {
  return tokens < 1000 ? String(tokens) : `${(tokens / 1000).toFixed(1)}k`;
}

/** 210 → "0.21 秒"; 5200 → "5.2 秒"; 84000 → "1 分 24 秒". */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${(ms / 1000).toFixed(2)} 秒`;
  if (ms < 60_000) return `${Number((ms / 1000).toFixed(1))} 秒`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

/** Local wall-clock time, HH:MM:SS. */
export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false });
}

/** Local date and time, YYYY/MM/DD HH:MM:SS. */
export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

export function elapsedMs(start: string, end: string | null): number | null {
  return end === null ? null : new Date(end).getTime() - new Date(start).getTime();
}

const STATUS: Record<string, string> = {
  pending: "等待中",
  running: "进行中",
  reported: "已报告",
  degraded: "已降级",
  interrupted: "已中断",
  failed: "失败",
};

export const statusLabel = (status: string) => STATUS[status] ?? status;

/** Whether an investigation has not ended yet. */
export const isOpenStatus = (status: string) => status === "pending" || status === "running";

const TRIGGER: Record<string, string> = { alert: "告警", manual: "手动", replay: "回放" };

export const triggerLabel = (trigger: string) => TRIGGER[trigger] ?? trigger;

const CATEGORY: Record<string, string> = {
  config_error: "配置错误",
  resource_limit: "资源限制",
  probe_misconfig: "探针配置",
  image_error: "镜像错误",
  dependency_unavailable: "依赖不可用",
  code_defect: "代码缺陷",
  other: "其他",
};

export const categoryLabel = (category: string) => CATEGORY[category] ?? category;

const CONFIDENCE: Record<string, string> = { high: "高", medium: "中", low: "低" };

export const confidenceLabel = (confidence: string) => CONFIDENCE[confidence] ?? confidence;

/** The profile a tool backend belongs to: real cluster or evidence pack. */
export const profileLabel = (toolBackend: string) =>
  toolBackend === "pack" ? "回放" : toolBackend === "live" ? "演示" : toolBackend;

export const agentLabel = (agent: string) =>
  agent === "investigation" ? "调查 Agent" : agent;
