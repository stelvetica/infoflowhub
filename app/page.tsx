import Link from "next/link";
import { deleteSourceAction, fetchNowAction, saveSourceAction, toggleSourceAction } from "@/app/actions";
import { EntriesSplitShell } from "@/app/components/entries-split-shell";
import { EntriesTableClient } from "@/app/components/entries-table-client";
import { LaterhubTableClient } from "@/app/components/laterhub-table-client";
import { SubmitButton } from "@/app/components/submit-button";
import { getEntriesView, getLaterhubView, getSettingsView } from "@/lib/data";
import { joinTags, normalizeText } from "@/lib/utils";
import type { ViewKey } from "@/lib/types";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

function one(value: string | string[] | undefined): string {
  return Array.isArray(value) ? value[0] || "" : value || "";
}

function href(view: ViewKey, params: Record<string, string>) {
  const search = new URLSearchParams({ view, ...Object.fromEntries(Object.entries(params).filter(([, value]) => value)) });
  return `/?${search.toString()}`;
}

function rootHref(params: Record<string, string>) {
  const search = new URLSearchParams(Object.fromEntries(Object.entries(params).filter(([, value]) => value)));
  const query = search.toString();
  return query ? `/?${query}` : "/";
}

function sortHref(view: ViewKey, currentSort: string, currentDir: string, nextSort: string, params: Record<string, string>, time = false) {
  const nextDir = time
    ? currentSort !== nextSort || currentDir === "asc"
      ? "desc"
      : "asc"
    : currentSort !== nextSort || currentDir === "desc"
      ? "asc"
      : "desc";
  return href(view, { ...params, sort: nextSort, dir: nextDir });
}

export default async function Home({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const view = (one(params.view) || "entries") as ViewKey;
  const showSettings = view === "settings";

  return (
    <div className="shell">
      <main className="main">
        <header className="topbar">
          <div className="brand brand-centered">
            <h1>InfoFlowHub</h1>
          </div>
          <Link className={`settings-link ${showSettings ? "active" : ""}`} href={showSettings ? "/" : "/?view=settings"} aria-label="设置">
            <span aria-hidden="true">⚙️</span>
          </Link>
        </header>
        {showSettings ? (
          <div className="stack">
            <SettingsPanel params={params} />
          </div>
        ) : (
          <EntriesSplitShell
            left={<EntriesPanel params={params} />}
            right={<LaterhubPanel params={params} />}
            initialCollapsed={one(params.laterhub_collapsed) === "1"}
          />
        )}
      </main>
    </div>
  );
}

async function EntriesPanel({ params }: { params: Record<string, string | string[] | undefined> }) {
  const laterhubCollapsed = one(params.laterhub_collapsed);
  const data = getEntriesView({
    q: one(params.entries_q),
    sort: one(params.entries_sort),
    dir: (one(params.entries_dir) || "desc") as "asc" | "desc",
    page: one(params.entries_page)
  });
  const baseParams = {
    entries_q: data.q,
    laterhub_q: one(params.laterhub_q),
    laterhub_filter_finished: one(params.laterhub_filter_finished),
    laterhub_filter_tag: one(params.laterhub_filter_tag),
    laterhub_sort: one(params.laterhub_sort),
    laterhub_dir: one(params.laterhub_dir),
    laterhub_collapsed: laterhubCollapsed
  };

  return (
    <section className="card pane-card pane-card-left">
      <div className="panel-title">
        <h3 className="panel-heading-nowrap">订阅内容</h3>
        <form className="toolbar toolbar-inline toolbar-inline-spread entries-header-form">
          <input type="hidden" name="entries_sort" value={data.sort} />
          <input type="hidden" name="entries_dir" value={data.dir} />
          <input type="hidden" name="laterhub_q" value={one(params.laterhub_q)} />
          <input type="hidden" name="laterhub_filter_finished" value={one(params.laterhub_filter_finished)} />
          <input type="hidden" name="laterhub_filter_tag" value={one(params.laterhub_filter_tag)} />
          <input type="hidden" name="laterhub_sort" value={one(params.laterhub_sort)} />
          <input type="hidden" name="laterhub_dir" value={one(params.laterhub_dir)} />
          <input type="hidden" name="laterhub_collapsed" value={laterhubCollapsed} />
          <div className="toolbar toolbar-inline">
            <input className="input" name="entries_q" defaultValue={data.q} placeholder="搜索标题、来源" />
            <button className="btn" type="submit">
              搜索
            </button>
          </div>
          <div id="entries-unread-slot" />
        </form>
      </div>
      <EntriesTableClient rows={data.rows} sort={data.sort} dir={data.dir} q={data.q} sharedParams={baseParams} />
    </section>
  );
}

async function LaterhubPanel({ params }: { params: Record<string, string | string[] | undefined> }) {
  const entriesQ = one(params.entries_q);
  const entriesSort = one(params.entries_sort);
  const entriesDir = one(params.entries_dir);
  const laterhubCollapsed = one(params.laterhub_collapsed);
  const data = getLaterhubView({
    q: one(params.laterhub_q),
    filter_finished: one(params.laterhub_filter_finished),
    filter_tag: one(params.laterhub_filter_tag),
    sort: one(params.laterhub_sort),
    dir: (one(params.laterhub_dir) || "desc") as "asc" | "desc"
  });

  function laterhubRoot(extra: Record<string, string> = {}) {
    return rootHref({
      entries_q: entriesQ,
      entries_sort: entriesSort,
      entries_dir: entriesDir,
      laterhub_q: data.q,
      laterhub_filter_finished: data.filterFinished,
      laterhub_filter_tag: joinTags(data.selectedTags),
      laterhub_sort: data.sort,
      laterhub_dir: data.dir,
      laterhub_collapsed: laterhubCollapsed,
      ...extra
    });
  }

  return (
    <div className="pane-right-stack">
      <section className="card pane-card pane-card-right">
        <div className="panel-title">
          <h3>稍后处理</h3>
        </div>
        <form className="laterhub-form-stack">
          <input type="hidden" name="entries_q" value={entriesQ} />
          <input type="hidden" name="entries_sort" value={entriesSort} />
          <input type="hidden" name="entries_dir" value={entriesDir} />
          <input type="hidden" name="laterhub_sort" value={data.sort} />
          <input type="hidden" name="laterhub_dir" value={data.dir} />
          <input type="hidden" name="laterhub_filter_tag" value={joinTags(data.selectedTags)} />
          <input type="hidden" name="laterhub_collapsed" value={laterhubCollapsed} />
          <div className="toolbar toolbar-inline laterhub-top-row">
            <input className="input laterhub-search-input" name="laterhub_q" defaultValue={data.q} placeholder="搜索标题或标签" />
            <button className="btn" type="submit">
              搜索
            </button>
            <select className="select laterhub-status-select laterhub-status-select-hidden" name="laterhub_filter_finished_disabled" defaultValue={data.filterFinished} disabled aria-hidden="true">
              <option value="0">未处理</option>
              <option value="1">已完成</option>
              <option value="">全部</option>
            </select>
            <Link
              className="btn laterhub-clear-btn"
              href={rootHref({
                entries_q: entriesQ,
                entries_sort: entriesSort,
                entries_dir: entriesDir,
                laterhub_collapsed: laterhubCollapsed
              })}
            >
              清空
            </Link>
          </div>
        </form>
        <div className="chips">
          <Link className={`chip ${data.selectedTags.length ? "" : "active"}`} href={laterhubRoot({ laterhub_filter_tag: "" })}>
            全部
          </Link>
          {data.allTags.map((tag) => {
            const key = normalizeText(tag);
            const active = data.selectedTags.some((item) => normalizeText(item) === key);
            const next = active ? data.selectedTags.filter((item) => normalizeText(item) !== key) : [...data.selectedTags, tag];
            return (
              <Link key={tag} className={`chip ${active ? "active" : ""}`} href={laterhubRoot({ laterhub_filter_tag: joinTags(next) })}>
                {tag}
              </Link>
            );
          })}
        </div>
        <div className="laterhub-summary-inline laterhub-summary-tight laterhub-summary-small">
          <div className="inline-metric">
            <span className="label">当前条目</span>
            <strong>{data.total}</strong>
          </div>
          <div className="inline-metric">
            <span className="label">已选标签</span>
            <strong>{data.selectedTags.length ? data.selectedTags.length : "无"}</strong>
          </div>
        </div>
        <div className="laterhub-filter-row">
          <span className="laterhub-filter-label">状态</span>
          <select className="select laterhub-status-select" name="laterhub_filter_finished" defaultValue={data.filterFinished}>
            <option value="">全部</option>
            <option value="1">已完成</option>
            <option value="0">未处理</option>
          </select>
        </div>
      </section>
      <section className="card pane-card pane-card-right">
        <LaterhubTableClient rows={data.rows} />
      </section>
    </div>
  );
}

async function SettingsPanel({ params }: { params: Record<string, string | string[] | undefined> }) {
  const data = getSettingsView({
    source_q: one(params.source_q),
    sort: one(params.sort),
    dir: (one(params.dir) || "asc") as "asc" | "desc"
  });
  const editId = one(params.edit_source);
  const editing = data.sources.find((item) => item.id === editId);

  return (
    <>
      <section className="card">
        <div className="panel-title">
          <h3>订阅设置</h3>
        </div>
        <div className="settings-topbar">
          <div className="metric-strip">
            <div className="metric-chip">
              <span className="label">最近执行</span>
              <strong>{data.status.last_run_at ? data.status.last_run_at.slice(5, 16) : "-"}</strong>
            </div>
            <div className="metric-chip">
              <span className="label">最近成功</span>
              <strong>{data.status.last_success_at ? data.status.last_success_at.slice(5, 16) : "-"}</strong>
            </div>
            <div className="metric-chip">
              <span className="label">抓取成功源</span>
              <strong>
                {data.status.last_success_sources}/{data.status.last_total_sources}
              </strong>
            </div>
            <div className="metric-chip">
              <span className="label">新增条目</span>
              <strong>{data.status.last_inserted_entries}</strong>
            </div>
          </div>
          <div className="settings-actions">
            <form action={fetchNowAction}>
              <SubmitButton idleText="立即抓取" pendingText="抓取中..." />
            </form>
            <a className="btn ghost" href="/api/restart-dev?returnTo=%2F%3Fview%3Dsettings">
              强制重启并清缓存
            </a>
          </div>
        </div>
        <div className="settings-error-block">
          <div className="subtle">错误详情</div>
          <pre className="codebox compact">{data.status.last_error || "无"}</pre>
        </div>
      </section>

      <section className="card">
        <div className="panel-title">
          <h3>稍后处理设置区</h3>
        </div>
        <div className="settings-summary-row">
          <p className="subtle">总数：{data.summary.total_count}</p>
          <p className="subtle">未完成：{data.summary.unfinished_count}</p>
          <p className="subtle">已完成：{data.summary.finished_count}</p>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>来源</th>
                <th>用途说明</th>
                <th>抓取方式</th>
                <th>总数</th>
                <th>未完成</th>
              </tr>
            </thead>
            <tbody>
              {data.laterhubSources.map((item) => (
                <tr key={item.source}>
                  <td>{item.label}</td>
                  <td>{item.purpose}</td>
                  <td>{item.fetch_mode}</td>
                  <td>{item.total_count}</td>
                  <td>{item.unfinished_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <div className="panel-title">
          <h3>订阅源管理</h3>
          <form className="toolbar">
            <input type="hidden" name="view" value="settings" />
            <input type="hidden" name="sort" value={data.sort} />
            <input type="hidden" name="dir" value={data.dir} />
            <input className="input" name="source_q" defaultValue={data.q} placeholder="搜索名称 / RSS URL" />
            <button className="btn" type="submit">
              搜索
            </button>
          </form>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>
                  <Link className="sort" href={sortHref("settings", data.sort, data.dir, "name", { source_q: data.q })}>
                    名称
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref("settings", data.sort, data.dir, "enabled_sort", { source_q: data.q })}>
                    生效
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref("settings", data.sort, data.dir, "provider_label", { source_q: data.q })}>
                    来源类型
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref("settings", data.sort, data.dir, "entry_count", { source_q: data.q }, true)}>
                    条目数
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref("settings", data.sort, data.dir, "invalid_sort", { source_q: data.q }, true)}>
                    失效天数
                  </Link>
                </th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {data.sources.map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>
                    <span className={`status ${item.enabled ? "ok" : "warn"}`}>{item.enabled_text}</span>
                  </td>
                  <td>{item.provider_label}</td>
                  <td>{item.entry_count}</td>
                  <td>{item.invalid_days || "-"}</td>
                  <td>
                    <div className="toolbar">
                      <form action={toggleSourceAction}>
                        <input type="hidden" name="source_id" value={item.id} />
                        <input type="hidden" name="enabled" value={item.enabled ? "0" : "1"} />
                        <SubmitButton className="btn secondary" idleText={item.enabled ? "停用" : "启用"} pendingText="处理中..." />
                      </form>
                      <Link className="btn ghost" href={href("settings", { edit_source: item.id, source_q: data.q, sort: data.sort, dir: data.dir })}>
                        编辑
                      </Link>
                      <form action={deleteSourceAction}>
                        <input type="hidden" name="source_id" value={item.id} />
                        <SubmitButton className="btn ghost" idleText="删除" pendingText="删除中..." />
                      </form>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <div className="panel-title">
          <h3>{editing ? "编辑订阅源" : "新增订阅源"}</h3>
          {editing ? (
            <Link className="btn ghost" href={href("settings", { source_q: data.q, sort: data.sort, dir: data.dir })}>
              取消编辑
            </Link>
          ) : null}
        </div>
        <form action={saveSourceAction} className="form-grid form-grid-inline">
          <input type="hidden" name="source_id" value={editing?.id || ""} />
          <label>
            名称
            <input className="input" name="name" defaultValue={editing?.name || ""} required />
          </label>
          <label>
            站点 URL
            <input className="input" name="site_url" defaultValue={editing?.site_url || ""} />
          </label>
          <label>
            RSS URL
            <input className="input" name="feed_url" defaultValue={editing?.feed_url || ""} required />
          </label>
          <div className="form-submit-inline">
            <SubmitButton idleText="保存" pendingText="保存中..." />
          </div>
        </form>
      </section>
    </>
  );
}
