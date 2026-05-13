"use client";

import Link from "next/link";
import { useState } from "react";
import { deleteSourceAction, saveSourceAction, toggleSourceAction } from "@/app/actions";
import { SubmitButton } from "@/app/components/submit-button";

type SourceRow = {
  id: string;
  name: string;
  enabled: boolean;
  enabled_text: string;
  provider_label: string;
  login_requirement: string;
  login_hint: string;
  entry_count: number;
  last_error: string;
  last_success_at: string;
  invalid_days: string;
  site_url: string;
  feed_url: string;
};

type SourcesSettingsClientProps = {
  q: string;
  sourceFilter: string;
  sort: string;
  dir: string;
  sources: SourceRow[];
};

type DraftSource = {
  id: string;
  name: string;
  site_url: string;
  feed_url: string;
};

const EMPTY_DRAFT: DraftSource = {
  id: "",
  name: "",
  site_url: "",
  feed_url: ""
};

function href(params: Record<string, string>) {
  const search = new URLSearchParams({
    view: "settings",
    ...Object.fromEntries(Object.entries(params).filter(([, value]) => value))
  });
  return `/?${search.toString()}`;
}

function sortHref(currentSort: string, currentDir: string, nextSort: string, params: Record<string, string>, time = false) {
  const nextDir = time
    ? currentSort !== nextSort || currentDir === "asc"
      ? "desc"
      : "asc"
    : currentSort !== nextSort || currentDir === "desc"
      ? "asc"
      : "desc";
  return href({ ...params, sort: nextSort, dir: nextDir });
}

export function SourcesSettingsClient({ q, sourceFilter, sort, dir, sources }: SourcesSettingsClientProps) {
  const [draft, setDraft] = useState<DraftSource | null>(null);

  function openCreate() {
    setDraft(EMPTY_DRAFT);
  }

  function openEdit(source: SourceRow) {
    setDraft({
      id: source.id,
      name: source.name,
      site_url: source.site_url || "",
      feed_url: source.feed_url || ""
    });
  }

  function closeModal() {
    setDraft(null);
  }

  return (
    <>
      <section className="card">
        <div className="panel-title">
          <h3>订阅源管理</h3>
          <form className="toolbar">
            <input type="hidden" name="view" value="settings" />
            <input type="hidden" name="source_filter" value={sourceFilter} />
            <input type="hidden" name="sort" value={sort} />
            <input type="hidden" name="dir" value={dir} />
            <input className="input" name="source_q" defaultValue={q} placeholder="搜索名称 / RSS URL" />
            <button className="btn" type="submit">
              搜索
            </button>
            <Link
              className={`btn ghost ${sourceFilter === "failed" ? "active-filter" : ""}`}
              href={href({ source_q: q, source_filter: sourceFilter === "failed" ? "" : "failed", sort, dir })}
            >
              {sourceFilter === "failed" ? "查看全部源" : "仅看失败/失效源"}
            </Link>
            <button className="btn secondary" type="button" onClick={openCreate}>
              新增订阅源
            </button>
          </form>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>
                  <Link className="sort" href={sortHref(sort, dir, "name", { source_q: q, source_filter: sourceFilter })}>
                    名称
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref(sort, dir, "enabled_sort", { source_q: q, source_filter: sourceFilter })}>
                    生效
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref(sort, dir, "provider_label", { source_q: q, source_filter: sourceFilter })}>
                    来源类型
                  </Link>
                </th>
                <th>登录要求</th>
                <th>
                  <Link className="sort" href={sortHref(sort, dir, "entry_count", { source_q: q, source_filter: sourceFilter }, true)}>
                    条目数
                  </Link>
                </th>
                <th>最近错误</th>
                <th>
                  <Link className="sort" href={sortHref(sort, dir, "last_success_at", { source_q: q, source_filter: sourceFilter }, true)}>
                    最近成功
                  </Link>
                </th>
                <th>
                  <Link className="sort" href={sortHref(sort, dir, "invalid_sort", { source_q: q, source_filter: sourceFilter }, true)}>
                    失效天数
                  </Link>
                </th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((item) => (
                <tr key={item.id}>
                  <td>{item.name}</td>
                  <td>
                    <span className={`status ${item.enabled ? "ok" : "warn"}`}>{item.enabled_text}</span>
                  </td>
                  <td>{item.provider_label}</td>
                  <td className="cell-login-requirement" title={item.login_hint || item.login_requirement || ""}>
                    {item.login_requirement ? <span className="requirement-badge">{item.login_requirement}</span> : "-"}
                  </td>
                  <td>{item.entry_count}</td>
                  <td className="cell-error-summary" title={item.last_error || ""}>
                    {item.last_error || "-"}
                  </td>
                  <td>{item.last_success_at ? item.last_success_at.slice(5, 16) : "-"}</td>
                  <td>{item.invalid_days || "-"}</td>
                  <td>
                    <div className="toolbar">
                      <form action={toggleSourceAction}>
                        <input type="hidden" name="source_id" value={item.id} />
                        <input type="hidden" name="enabled" value={item.enabled ? "0" : "1"} />
                        <SubmitButton className="btn secondary" idleText={item.enabled ? "停用" : "启用"} pendingText="处理中..." />
                      </form>
                      <button className="btn ghost" type="button" onClick={() => openEdit(item)}>
                        编辑
                      </button>
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

      {draft ? (
        <div className="modal-backdrop" role="presentation" onClick={closeModal}>
          <div className="modal-card" role="dialog" aria-modal="true" aria-labelledby="source-modal-title" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <h3 id="source-modal-title">{draft.id ? "编辑订阅源" : "新增订阅源"}</h3>
              <button className="btn ghost" type="button" onClick={closeModal}>
                关闭
              </button>
            </div>
            <form action={saveSourceAction} className="form-grid modal-form-grid">
              <input type="hidden" name="source_id" value={draft.id} />
              <label>
                名称
                <input className="input" name="name" defaultValue={draft.name} required />
              </label>
              <label>
                站点 URL
                <input className="input" name="site_url" defaultValue={draft.site_url} />
              </label>
              <label>
                RSS URL
                <input className="input" name="feed_url" defaultValue={draft.feed_url} required />
              </label>
              <div className="modal-actions">
                <button className="btn ghost" type="button" onClick={closeModal}>
                  取消
                </button>
                <SubmitButton className="btn" idleText="保存" pendingText="保存中..." />
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </>
  );
}
