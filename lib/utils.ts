export function normalizeText(value: string): string {
  return stripInvalidUnicode(value).trim().toLowerCase();
}

export function stripInvalidUnicode(value: string): string {
  return (value || "").replace(/[\uD800-\uDFFF]/g, "");
}

export function splitTags(value: string | null | undefined): string[] {
  const text = stripInvalidUnicode(value || "").trim();
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
  return tags.map((item) => stripInvalidUnicode(item).trim()).filter(Boolean).join(",");
}

export function formatDateTime(value: string): string {
  const parsed = parseDateTime(value);
  if (!parsed) return stripInvalidUnicode(value || "").trim().slice(0, 16).replaceAll("-", "/");
  return `${parsed.getFullYear()}/${String(parsed.getMonth() + 1).padStart(2, "0")}/${String(parsed.getDate()).padStart(2, "0")} ${String(parsed.getHours()).padStart(2, "0")}:${String(parsed.getMinutes()).padStart(2, "0")}`;
}

export function buildSourceId(name: string): string {
  const value = stripInvalidUnicode(name)
    .trim()
    .toLowerCase()
    .replace(/[ /\\:，（）,()]+/g, "-")
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
  const parsed = parseDateTime(value);
  return parsed ? parsed.getTime() : 0;
}

function parseDateTime(value: string): Date | null {
  const text = stripInvalidUnicode(value || "").trim();
  if (!text) return null;

  const normalized = text
    .replace(/[年-]/g, "/")
    .replace(/月/g, "/")
    .replace(/日/g, "")
    .replace(/\s+/g, " ")
    .trim();

  const match = normalized.match(/^(\d{4})\/(\d{1,2})\/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$/);
  if (match) {
    const [, year, month, day, hour = "0", minute = "0"] = match;
    const parsed = new Date(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      0,
      0
    );
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  const fallback = new Date(normalized.replace(" ", "T"));
  return Number.isNaN(fallback.getTime()) ? null : fallback;
}

export function compareValue(a: unknown, b: unknown, direction: "asc" | "desc"): number {
  const factor = direction === "asc" ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return (a - b) * factor;
  return String(a ?? "").localeCompare(String(b ?? ""), "zh-CN") * factor;
}
