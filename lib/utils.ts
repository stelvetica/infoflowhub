export function normalizeText(value: string): string {
  return value.trim().toLowerCase();
}

export function splitTags(value: string | null | undefined): string[] {
  const text = (value || "").trim();
  if (!text) return [];
  const parts = text
    .replaceAll("、", ",")
    .replaceAll("，", ",")
    .replaceAll("；", ",")
    .replaceAll(";", ",")
    .replaceAll("|", ",")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  return [...new Map(parts.map((item) => [normalizeText(item), item])).values()];
}

export function joinTags(tags: string[]): string {
  return tags.map((item) => item.trim()).filter(Boolean).join(",");
}

export function formatDateTime(value: string): string {
  const text = (value || "").trim();
  if (!text) return "";
  const parsed = new Date(text.replace(" ", "T"));
  if (Number.isNaN(parsed.getTime())) return text.slice(0, 16).replaceAll("-", "/");
  return `${parsed.getFullYear()}/${String(parsed.getMonth() + 1).padStart(2, "0")}/${String(parsed.getDate()).padStart(2, "0")} ${String(parsed.getHours()).padStart(2, "0")}:${String(parsed.getMinutes()).padStart(2, "0")}`;
}

export function buildSourceId(name: string): string {
  const value = name
    .trim()
    .toLowerCase()
    .replace(/[ /\\:，（），,()]+/g, "-")
    .split("-")
    .filter(Boolean)
    .join("-");
  return value || `source-${Date.now()}`;
}

export function providerLabel(provider: string, fetchVia: string): string {
  if (provider === "rsshub") {
    return fetchVia === "rsshub-public" ? "RSSHub 公共" : "RSSHub";
  }
  if (provider === "web") return "网页直抓";
  return "原生 RSS";
}

export function toSortableTime(value: string): number {
  const parsed = new Date(value.replace(" ", "T"));
  return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
}

export function compareValue(a: unknown, b: unknown, direction: "asc" | "desc"): number {
  const factor = direction === "asc" ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return (a - b) * factor;
  return String(a ?? "").localeCompare(String(b ?? ""), "zh-CN") * factor;
}
