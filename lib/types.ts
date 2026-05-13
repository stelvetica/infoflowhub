export type ViewKey = "entries" | "laterhub" | "settings";

export type SubscriptionsEntry = {
  source_id: string;
  source_name: string;
  title: string;
  link: string;
  published: string;
  published_at?: string;
  summary: string;
  created_at: string;
};

export type LaterhubItem = {
  id: number;
  url: string;
  title: string;
  tags: string | null;
  created_at: string;
  updated_at: string;
  is_finished: number;
};

export type SourceItem = {
  id: string;
  name: string;
  group: string;
  feed_url: string;
  site_url: string;
  provider: string;
  fetch_via: string;
  kind: string;
  enabled: boolean;
  note: string;
  login_requirement?: string;
  login_hint?: string;
};

export type SourceHealth = {
  source_name: string;
  feed_url: string;
  last_checked_at: string;
  last_success_at: string;
  last_failed_at: string;
  last_error: string;
};

export type RuntimeStatus = {
  fetch_state: "idle" | "running" | "success" | "error";
  current_run_started_at: string;
  last_run_at: string;
  last_success_at: string;
  last_error: string;
  last_total_sources: number;
  last_success_sources: number;
  last_inserted_entries: number;
};

export type LaterhubSummary = {
  total_count: number;
  unfinished_count: number;
  finished_count: number;
};

export type LaterhubSourceStats = {
  source: string;
  label: string;
  purpose: string;
  fetch_mode: string;
  total_count: number;
  finished_count: number;
  unfinished_count: number;
};
