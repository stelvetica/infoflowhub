import fs from "node:fs";
import path from "node:path";
import { runPythonBridge } from "@/lib/python-bridge";
import { buildSourceId, compareValue, formatDateTime, normalizeText, providerLabel, splitTags, toSortableTime } from "@/lib/utils";
import type {
  LaterhubSourceStats,
  LaterhubSummary,
  RuntimeStatus,
  SourceHealth,
  SourceItem,
  SubscriptionsEntry
} from "@/lib/types";

const CONFIG_DIR = path.join(process.cwd(), "config");
const RUNTIME_DIR = path.join(process.cwd(), "runtime");
const SOURCES_PATH = path.join(CONFIG_DIR, "rss_sources.json");
const SETTINGS_PATH = path.join(CONFIG_DIR, "rss_settings.json");
const STATUS_PATH = path.join(RUNTIME_DIR, "health", "subscriptions_status.json");
const HEALTH_PATH = path.join(RUNTIME_DIR, "health", "subscriptions_source_health.json");

type SourcesPayload = { sources: SourceItem[] };
type HealthPayload = { sources: Record<string, SourceHealth> };
type SnapshotPayload = {
  entries: SubscriptionsEntry[];
  entries_total: number;
  source_stats: Array<{ source_id: string; source_name: string; entry_count: number; last_seen: string }>;
  laterhub_items: Array<{
    id: number;
    url: string;
    title: string;
    tags: string | null;
    created_at: string;
    updated_at: string;
    is_finished: number;
  }>;
  laterhub_total: number;
  laterhub_summary: LaterhubSummary;
  laterhub_source_stats: LaterhubSourceStats[];
};

type EntriesSnapshotPayload = {
  entries: SubscriptionsEntry[];
  entries_total: number;
};

type LaterhubSnapshotPayload = {
  laterhub_items: Array<{
    id: number;
    url: string;
    title: string;
    tags: string | null;
    created_at: string;
    updated_at: string;
    is_finished: number;
  }>;
  laterhub_total: number;
  laterhub_summary: LaterhubSummary;
  laterhub_source_stats: LaterhubSourceStats[];
};

type SettingsSnapshotPayload = {
  source_stats: Array<{ source_id: string; source_name: string; entry_count: number; last_seen: string }>;
  laterhub_summary: LaterhubSummary;
  laterhub_source_stats: LaterhubSourceStats[];
};

function readJson<T>(filePath: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf-8")) as T;
  } catch {
    return fallback;
  }
}

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf-8");
}

const webFeedUrls = new Set([
  "https://rsshub.app/bilibili/user/dynamic/14089380",
  "https://rsshub.app/bilibili/user/dynamic/474921808",
  "https://rsshub.app/bilibili/user/dynamic/162183",
  "https://rsshub.app/bilibili/user/dynamic/1908067732",
  "https://rsshub.app/bilibili/user/dynamic/2117498259",
  "https://rsshub.app/bilibili/user/dynamic/472747194",
  "https://rsshub.app/bilibili/user/dynamic/316183842",
  "https://rsshub.app/bilibili/user/dynamic/1257954297",
  "https://rsshub.app/bilibili/user/dynamic/381870733",
  "https://rsshub.app/bilibili/user/video/3546909549529340",
  "https://rsshub.app/bilibili/user/dynamic/2233213",
  "https://rsshub.app/weibo/user/2014433131",
  "https://rsshub.app/weibo/user/7782629809",
  "https://rsshub.app/twitter/user/MacroMargin"
]);

const nativeFeedUrls = new Set([
  "https://www.supertechfans.com/cn/index.xml",
  "https://www.youtube.com/feeds/videos.xml?channel_id=UC7eBKmeAz99qswOcm3VxOow",
  "https://www.youtube.com/feeds/videos.xml?channel_id=UC8gZZWIWmBuCb_gzC8DUrvw",
  "https://feeds.feedburner.com/ruanyifeng",
  "https://lumina.shawnxie.top/backend/api/reviews/rss.xml"
]);

const aliasRules = new Map<string, Partial<SourceItem>>([
  ["https://rsshub.app/bilibili/user/video/3546909549529340", { name: "外资观点 小鹿投研日记 的 bilibili 动态", site_url: "https://space.bilibili.com/3546909549529340" }],
  ["https://rsshub.app/bilibili/user/dynamic/14089380", { name: "技术 算法 labuladong 的 bilibili 动态", site_url: "https://space.bilibili.com/14089380/dynamic" }],
  ["https://rsshub.app/bilibili/user/dynamic/1908067732", { name: "观点 路口大爷聊宏观 的 bilibili 动态", site_url: "https://space.bilibili.com/1908067732/dynamic" }],
  ["https://rsshub.app/bilibili/user/dynamic/2117498259", { name: "基金 硬核姬老板 的 bilibili 动态", site_url: "https://space.bilibili.com/2117498259/dynamic" }],
  ["https://rsshub.app/bilibili/user/dynamic/472747194", { name: "产业 科普 巫师财经 的 bilibili 动态", site_url: "https://space.bilibili.com/472747194/dynamic" }],
  ["https://rsshub.app/bilibili/user/dynamic/1257954297", { name: "观点 房产 铁锤观察室 的 bilibili 动态", site_url: "https://space.bilibili.com/1257954297/dynamic" }],
  ["https://rsshub.app/bilibili/user/dynamic/381870733", { name: "外资观点 小黄的投资笔记 的 bilibili 动态", site_url: "https://space.bilibili.com/381870733/dynamic" }],
  ["https://rsshub.app/bilibili/user/dynamic/2233213", { name: "时事 短评 长文视频 星话大白 的 bilibili 动态", site_url: "https://space.bilibili.com/2233213/dynamic" }],
  ["https://lumina.shawnxie.top/backend/api/reviews/rss.xml", { name: "技术 肖恩周刊", site_url: "https://lumina.shawnxie.top/" }]
]);

const deletedSiteUrls = new Set(["https://www.huxiu.com/member/2321131.html"]);

export function loadSettings(): Record<string, unknown> {
  return readJson<Record<string, unknown>>(SETTINGS_PATH, {
    rsshub: { public_base: "https://rsshub.app", self_hosted_base: "", prefer_self_hosted: false }
  });
}

export function loadStatus(): RuntimeStatus {
  return readJson<RuntimeStatus>(STATUS_PATH, {
    last_run_at: "",
    last_success_at: "",
    last_error: "",
    last_total_sources: 0,
    last_success_sources: 0,
    last_inserted_entries: 0
  });
}

export function loadHealth(): HealthPayload {
  return readJson<HealthPayload>(HEALTH_PATH, { sources: {} });
}

function canonicalFeedUrl(source: SourceItem): string {
  if (source.site_url && source.provider === "web") {
    return source.site_url.trim();
  }
  return source.feed_url.trim();
}

export function normalizeSources(): SourceItem[] {
  const payload = readJson<SourcesPayload>(SOURCES_PATH, { sources: [] });
  const health = loadHealth();
  const changed: SourceItem[] = [];
  const seenIds = new Set<string>();
  const seenFeeds = new Set<string>();

  for (const item of payload.sources) {
    const feedUrl = (item.feed_url || "").trim();
    const siteUrl = (item.site_url || "").trim();
    if (!feedUrl || deletedSiteUrls.has(siteUrl)) continue;
    const source: SourceItem = {
      ...item,
      id: (item.id || "").trim(),
      name: (item.name || "").trim(),
      feed_url: feedUrl,
      site_url: siteUrl,
      enabled: Boolean(item.enabled ?? true),
      provider: item.provider || "native",
      fetch_via: item.fetch_via || "direct",
      kind: item.kind || item.provider || "native",
      group: item.group || "手动新增",
      note: item.note || ""
    };
    const alias = aliasRules.get(feedUrl);
    if (alias) Object.assign(source, alias);
    if ((feedUrl + siteUrl).toLowerCase().includes("bilibili.com") && !source.name.endsWith("bilibili 动态")) {
      source.name = `${source.name} bilibili 动态`;
    }
    if (nativeFeedUrls.has(feedUrl)) {
      source.provider = "native";
      source.fetch_via = "direct";
      source.kind = "native";
    } else if (webFeedUrls.has(feedUrl)) {
      source.provider = "web";
      source.fetch_via = "web";
      source.kind = "web";
    } else if (source.kind === "rsshub") {
      source.provider = "rsshub";
      if (!["rsshub-self-hosted", "rsshub-public"].includes(source.fetch_via)) source.fetch_via = "rsshub-self-hosted";
    } else if (source.kind === "web") {
      source.provider = "web";
      source.fetch_via = "web";
      source.kind = "web";
    } else {
      source.provider = "native";
      source.fetch_via = "direct";
      source.kind = "native";
    }
    if (!source.id) source.id = buildSourceId(source.name);
    if (seenIds.has(source.id) || seenFeeds.has(source.feed_url)) continue;
    seenIds.add(source.id);
    seenFeeds.add(source.feed_url);
    if (health.sources[source.id]) {
      health.sources[source.id].source_name = source.name;
      health.sources[source.id].feed_url = canonicalFeedUrl(source);
      if (typeof source.enabled !== "boolean") source.enabled = true;
    }
    changed.push(source);
  }
  if (changed.length !== payload.sources.length) saveSources(changed);
  writeJson(HEALTH_PATH, health);
  return changed;
}

export function saveSources(sources: SourceItem[]): void {
  writeJson(SOURCES_PATH, { sources });
}

export function saveSource(input: { source_id?: string; name: string; feed_url: string; site_url?: string }): void {
  const existing = normalizeSources().find((item) => item.id === input.source_id);
  const provider = existing?.provider || (input.feed_url.includes("rsshub") ? "rsshub" : "native");
  const fetchVia = existing?.fetch_via || (provider === "rsshub" ? "rsshub-self-hosted" : "direct");
  const kind = provider === "rsshub" ? "rsshub" : provider === "web" ? "web" : "native";
  const target: SourceItem = {
    id: input.source_id?.trim() || buildSourceId(input.name),
    name: input.name.trim(),
    group: existing?.group || "手动新增",
    feed_url: input.feed_url.trim(),
    site_url: (input.site_url || "").trim(),
    provider,
    fetch_via: fetchVia,
    kind,
    enabled: existing ? existing.enabled : true,
    note: existing?.note || ""
  };
  if (!target.name || !target.feed_url) return;
  const sources = normalizeSources();
  const next = sources.some((item) => item.id === target.id) ? sources.map((item) => (item.id === target.id ? target : item)) : [...sources, target];
  saveSources(next);
  runPythonBridge("set-source-enabled", { source_id: target.id, enabled: target.enabled });
}

export function toggleSource(sourceId: string, enabled: boolean): void {
  const next = normalizeSources().map((item) => (item.id === sourceId ? { ...item, enabled } : item));
  saveSources(next);
  runPythonBridge("set-source-enabled", { source_id: sourceId, enabled });
}

export function deleteSource(sourceId: string): void {
  saveSources(normalizeSources().filter((item) => item.id !== sourceId));
  runPythonBridge("delete-source-data", { source_id: sourceId });
  const health = loadHealth();
  if (health.sources[sourceId]) {
    delete health.sources[sourceId];
    writeJson(HEALTH_PATH, health);
  }
}

export function markLaterhubFinished(linkId: number, finished: boolean): void {
  runPythonBridge("mark-laterhub-finished", { id: linkId, finished });
}

function loadSnapshot(): SnapshotPayload {
  return runPythonBridge("snapshot") as SnapshotPayload;
}

function loadEntriesSnapshot(): EntriesSnapshotPayload {
  return runPythonBridge("entries-snapshot") as EntriesSnapshotPayload;
}

function loadLaterhubSnapshot(): LaterhubSnapshotPayload {
  return runPythonBridge("laterhub-snapshot") as LaterhubSnapshotPayload;
}

function loadSettingsSnapshot(): SettingsSnapshotPayload {
  return runPythonBridge("settings-snapshot") as SettingsSnapshotPayload;
}

export type EntriesQuery = { q?: string; sort?: string; dir?: "asc" | "desc"; page?: string };
export type LaterhubQuery = { q?: string; filter_finished?: string; filter_tag?: string; sort?: string; dir?: "asc" | "desc" };
export type SourcesQuery = { source_q?: string; sort?: string; dir?: "asc" | "desc" };

const ENTRIES_PAGE_SIZE = 50;

export function getEntriesView(query: EntriesQuery) {
  const snapshot = loadEntriesSnapshot();
  const enabledIds = new Set(normalizeSources().filter((item) => item.enabled).map((item) => item.id));
  const keyword = normalizeText(query.q || "");
  const sort = query.sort || "sort_time";
  const dir = query.dir || "desc";
  const page = Math.max(Number.parseInt(query.page || "1", 10) || 1, 1);
  const filteredRows = snapshot.entries
    .filter((item) => enabledIds.has(item.source_id))
    .filter((item) => !keyword || [item.source_name, item.title, item.summary].some((field) => normalizeText(field).includes(keyword)))
    .map((item) => ({
      ...item,
      summary: "",
      display_time: formatDateTime(item.published_at || item.published || item.created_at),
      sort_time: toSortableTime(item.published_at || item.published || item.created_at)
    }))
    .sort((a, b) => compareValue(a[sort as keyof typeof a], b[sort as keyof typeof b], dir));
  const filteredTotal = filteredRows.length;
  const totalPages = Math.max(Math.ceil(filteredTotal / ENTRIES_PAGE_SIZE), 1);
  const safePage = Math.min(page, totalPages);
  const start = (safePage - 1) * ENTRIES_PAGE_SIZE;
  const rows = filteredRows.slice(start, start + ENTRIES_PAGE_SIZE);
  return { rows, total: snapshot.entries_total, filteredTotal, totalPages, page: safePage, sort, dir, q: query.q || "" };
}

export function getLaterhubView(query: LaterhubQuery) {
  const snapshot = loadLaterhubSnapshot();
  const sort = query.sort || "sort_time";
  const dir = query.dir || "desc";
  const keyword = normalizeText(query.q || "");
  const filterFinished = query.filter_finished ?? "0";
  const selectedTags = splitTags(query.filter_tag || "");
  const selectedTagKeys = new Set(selectedTags.map(normalizeText));
  const allRows = snapshot.laterhub_items.map((item) => {
    const tags = item.tags || "";
    return {
      ...item,
      display_time: formatDateTime(item.created_at),
      sort_time: toSortableTime(item.created_at),
      finished_text: item.is_finished ? "已完成" : "未完成",
      tags_text: tags,
      tag_list: splitTags(tags),
      tag_keys: new Set(splitTags(tags).map(normalizeText))
    };
  });
  const rows = allRows
    .filter((item) => !keyword || normalizeText(item.title).includes(keyword) || normalizeText(item.tags_text).includes(keyword))
    .filter((item) => (filterFinished === "1" ? Boolean(item.is_finished) : filterFinished === "0" ? !item.is_finished : true))
    .filter((item) => [...selectedTagKeys].every((tag) => item.tag_keys.has(tag)))
    .sort((a, b) => compareValue(a[sort as keyof typeof a], b[sort as keyof typeof b], dir));
  const allTags = [...new Map(allRows.flatMap((item) => item.tag_list).map((tag) => [normalizeText(tag), tag])).values()].sort((a, b) =>
    a.localeCompare(b, "zh-CN")
  );
  return { rows, total: snapshot.laterhub_total, allTags, selectedTags, sort, dir, q: query.q || "", filterFinished };
}

export function getSettingsView(query: SourcesQuery) {
  const snapshot = loadSettingsSnapshot();
  const status = loadStatus();
  const summary = snapshot.laterhub_summary;
  const laterhubSources = snapshot.laterhub_source_stats;
  const health = loadHealth().sources;
  const stats = Object.fromEntries(snapshot.source_stats.map((item) => [item.source_id, item]));
  const keyword = normalizeText(query.source_q || "");
  const sort = query.sort || "name";
  const dir = query.dir || "asc";
  const sources = normalizeSources()
    .map((item) => {
      const stat = stats[item.id];
      const sourceHealth = health[item.id];
      const failedAt = sourceHealth?.last_failed_at ? new Date(sourceHealth.last_failed_at.replace(" ", "T")) : null;
      const successAt = sourceHealth?.last_success_at ? new Date(sourceHealth.last_success_at.replace(" ", "T")) : null;
      const invalidDays =
        failedAt && (!successAt || failedAt > successAt) ? String(Math.max(Math.floor((Date.now() - failedAt.getTime()) / 86400000), 0)) : "";
      return {
        ...item,
        provider_label: providerLabel(item.provider, item.fetch_via),
        entry_count: stat?.entry_count || 0,
        invalid_days: invalidDays,
        invalid_sort: invalidDays ? Number(invalidDays) : -1,
        enabled_sort: item.enabled ? 1 : 0,
        enabled_text: item.enabled ? "生效" : "停用"
      };
    })
    .filter((item) => !keyword || [item.name, item.feed_url, item.site_url].some((field) => normalizeText(field).includes(keyword)))
    .sort((a, b) => compareValue(a[sort as keyof typeof a], b[sort as keyof typeof b], dir));
  return { status, summary, laterhubSources, sources, q: query.source_q || "", sort, dir };
}
