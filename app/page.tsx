import Link from "next/link";
import { deleteSourceAction, fetchNowAction, finishLaterhubAction, saveSourceAction, toggleSourceAction } from "@/app/actions";
import { EntriesTableClient } from "@/app/components/entries-table-client";
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

function pageHref(view: ViewKey, params: Record<string, string>, page: number) {
  return href(view, { ...params, page: String(page) });
}

export default async function Home({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const view = (one(params.view) || "entries") as ViewKey;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <h1>InfoFlowHub</h1>
        </div>
        <nav className="nav">
          <Link href="/?view=entries" className={view === "entries" ? "active" : ""}>
            订阅内容
          </Link>
          <Link href="/?view=laterhub" className={view === "laterhub" ? "active" : ""}>
            稍后处理
          </Link>
          <Link href="/?view=settings" className={view === "settings" ? "active" : ""}>
            设置
          </Link>
        </nav>
      </aside>
      <main className="main">
        <div className="stack">{view === "entries" ? <EntriesPanel params={params} /> : view === "laterhub" ? <LaterhubPanel params={params} /> : <SettingsPanel params={params} />}</div>
      </main>
    </div>
  );
}

async function EntriesPanel({ params }: { params: Record<string, string | string[] | undefined> }) {
  const data = getEntriesView({
    q: one(params.q),
    sort: one(params.sort),
    dir: (one(params.dir) || "desc") as "asc" | "desc",
    page: one(params.page)
  });
  const baseParams = { q: data.q };

  return (
    <section className="card">
      <div className="panel-title">
        <h3>订阅内容</h3>
        <form className="toolbar">
          <input type="hidden" name="view" value="entries" />
          <input type="hidden" name="sort" value={data.sort} />
          <input type="hidden" name="dir" value={data.dir} />
          <input className="input" name="q" defaultValue={data.q} placeholder="搜索标题、来源" />
          <button className="btn" type="submit">
            搜索
          </button>
        </form>
      </div>
      <EntriesTableClient rows={data.rows} sort={data.sort} dir={data.dir} q={data.q} />
      <div className="pagination">
        <div className="subtle">
          第 {data.page} / {data.totalPages} 页，共 {data.filteredTotal} 条
        </div>
        <div className="toolbar">
          <Link className={`btn ghost ${data.page <= 1 ? "disabled" : ""}`} href={data.page > 1 ? pageHref("entries", { ...baseParams, sort: data.sort, dir: data.dir }, data.page - 1) : "#"} aria-disabled={data.page <= 1}>
            上一页
          </Link>
          <Link className={`btn ghost ${data.page >= data.totalPages ? "disabled" : ""}`} href={data.page < data.totalPages ? pageHref("entries", { ...baseParams, sort: data.sort, dir: data.dir }, data.page + 1) : "#"} aria-disabled={data.page >= data.totalPages}>
            下一页
          </Link>
        </div>
      </div>
    </section>
  );
}

async function LaterhubPanel({ params }: { params: Record<string, string | string[] | undefined> }) {
  const data = getLaterhubView({
    q: one(params.q),
    filter_finished: one(params.filter_finished),
    filter_tag: one(params.filter_tag),
    sort: one(params.sort),
    dir: (one(params.dir) || "desc") as "asc" | "desc"
  });

  return (
    <>
      <section className="card">
        <div className="panel-title">
          <h3>筛选</h3>
        </div>
        <form className="filterbar">
          <input type="hidden" name="view" value="laterhub" />
          <input type="hidden" name="sort" value={data.sort} />
          <input type="hidden" name="dir" value={data.dir} />
          <input className="input" name="q" defaultValue={data.q} placeholder="搜索标题或标签" />
          <select className="select" name="filter_finished" defaultValue={data.filterFinished}>
            <option value="0">未完成</option>
            <option value="1">已完成</option>
            <option value="">全部</option>
          </select>
          <input type="hidden" name="filter_tag" value={joinTags(data.selectedTags)} />
          <button className="btn" type="submit">
            搜索
          </button>
          <Link className="btn ghost" href="/?view=laterhub">
            清空
          </Link>
        </form>
      </section>
      <section className="card">
        <div className="panel-title">
          <h3>标签</h3>
          <div className="subtle">已选：{data.selectedTags.length ? data.selectedTags.join("、") : "无"}</div>
        </div>
        <div className="chips">
          <Link className={`chip ${data.selectedTags.length ? "" : "active"}`} href={href("laterhub", { q: data.q, filter_finished: data.filterFinished, sort: data.sort, dir: data.dir, filter_tag: "" })}>
            全部
          </Link>
          {data.allTags.map((tag) => {
            const key = normalizeText(tag);
            const active = data.selectedTags.some((item) => normalizeText(item) === key);
            const next = active ? data.selectedTags.filter((item) => normalizeText(item) !== key) : [...data.selectedTags, tag];
            return (
              <Link key={tag} className={`chip ${active ? "active" : ""}`} href={href("laterhub", { q: data.q, filter_finished: data.filterFinished, sort: data.sort, dir: data.dir, filter_tag: joinTags(next) })}>
                {tag}
              </Link>
            );
          })}
        </div>
        <div className="laterhub-summary-inline">
          <div className="inline-metric">
            <span className="label">当前条目</span>
            <strong>{data.total}</strong>
          </div>
          <div className="inline-metric">
            <span className="label">已选标签</span>
            <strong>{data.selectedTags.length ? data.selectedTags.length : "无"}</strong>
          </div>
          <div className="inline-metric">
            <span className="label">完成筛选</span>
            <strong>{data.filterFinished === "1" ? "已完成" : data.filterFinished === "0" ? "未完成" : "全部"}</strong>
          </div>
        </div>
      </section>
      <section className="card">
        <div className="table-wrap">
          <table className="laterhub-table">
            <colgroup>
              <col className="col-time" />
              <col className="col-title" />
              <col className="col-action" />
            </colgroup>
            <thead>
              <tr>
                <th>
                  <Link className="sort" href={sortHref("laterhub", data.sort, data.dir, "sort_time", { q: data.q, filter_finished: data.filterFinished, filter_tag: joinTags(data.selectedTags) }, true)}>
                    时间
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref("laterhub", data.sort, data.dir, "title", { q: data.q, filter_finished: data.filterFinished, filter_tag: joinTags(data.selectedTags) })}>
                    链接
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref("laterhub", data.sort, data.dir, "finished_text", { q: data.q, filter_finished: data.filterFinished, filter_tag: joinTags(data.selectedTags) })}>
                    完成
                  </Link>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.rows.length ? (
                data.rows.map((item) => (
                  <tr key={item.id}>
                    <td className="cell-time">{item.display_time}</td>
                    <td className="laterhub-link-cell">
                      <div className="cell-ellipsis" title={item.title}>
                        <a className="cell-link cell-strong" href={item.url} target="_blank" rel="noreferrer">
                          {item.title}
                        </a>
                      </div>
                      <div className="subtle cell-ellipsis" title={item.tags_text || "-"}>
                        {item.tags_text || "-"}
                      </div>
                    </td>
                    <td className="laterhub-action-cell">
                      <form action={finishLaterhubAction}>
                        <input type="hidden" name="id" value={item.id} />
                        <input type="hidden" name="finished" value={item.is_finished ? "0" : "1"} />
                        <SubmitButton className={`btn ${item.is_finished ? "secondary" : ""}`} idleText={item.is_finished ? "标记未完成" : "标记完成"} pendingText="提交中..." />
                      </form>
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={3} className="empty">
                    没有匹配内容
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </>
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
