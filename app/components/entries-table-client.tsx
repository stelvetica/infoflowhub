"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { formatDate } from "@/lib/utils";

type EntryRow = {
  source_id: string;
  source_name: string;
  title: string;
  link: string;
  display_time: string;
};

type EntriesTableClientProps = {
  rows: EntryRow[];
  sort: string;
  dir: "asc" | "desc";
  q: string;
  sharedParams?: Record<string, string>;
};

const STORAGE_KEY = "infoflowhub:read-links";

function sortHref(currentSort: string, currentDir: string, nextSort: string, q: string, sharedParams: Record<string, string>, time = false) {
  const nextDir = time
    ? currentSort !== nextSort || currentDir === "asc"
      ? "desc"
      : "asc"
    : currentSort !== nextSort || currentDir === "desc"
      ? "asc"
      : "desc";
  const search = new URLSearchParams({ ...sharedParams, entries_sort: nextSort, entries_dir: nextDir });
  if (q) search.set("entries_q", q);
  return `/?${search.toString()}`;
}

export function EntriesTableClient({ rows, sort, dir, q, sharedParams = {} }: EntriesTableClientProps) {
  const [showUnreadOnly, setShowUnreadOnly] = useState(true);
  const [readLinks, setReadLinks] = useState<Record<string, boolean>>({});
  const [unreadSlot, setUnreadSlot] = useState<HTMLElement | null>(null);
  const [page, setPage] = useState(1);
  const pageSize = 35;

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      setReadLinks(JSON.parse(raw) as Record<string, boolean>);
    } catch {
      setReadLinks({});
    }
  }, []);

  useEffect(() => {
    setUnreadSlot(document.getElementById("entries-unread-slot"));
  }, []);

  function markRead(link: string) {
    setReadLinks((current) => {
      if (current[link]) return current;
      const next = { ...current, [link]: true };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  }

  const visibleRows = useMemo(() => (showUnreadOnly ? rows.filter((item) => !readLinks[item.link]) : rows), [rows, showUnreadOnly, readLinks]);
  const totalPages = Math.max(Math.ceil(visibleRows.length / pageSize), 1);
  const safePage = Math.min(page, totalPages);
  const pagedRows = useMemo(() => visibleRows.slice((safePage - 1) * pageSize, safePage * pageSize), [visibleRows, safePage]);

  useEffect(() => {
    setPage(1);
  }, [showUnreadOnly, rows]);

  return (
    <>
      {unreadSlot
        ? createPortal(
            <button className={`btn ghost ${showUnreadOnly ? "active-filter" : ""}`} type="button" onClick={() => setShowUnreadOnly((current) => !current)}>
              {showUnreadOnly ? "显示全部" : "未读"}
            </button>,
            unreadSlot
          )
        : null}
      <div className="table-wrap">
        <table className="entries-table">
          <colgroup>
            <col className="col-time" />
            <col className="col-source" />
            <col className="col-title" />
          </colgroup>
          <thead>
            <tr>
              <th>
                <Link className="sort" href={sortHref(sort, dir, "sort_time", q, sharedParams, true)}>
                  时间
                </Link>
              </th>
              <th>
                <Link className="sort" href={sortHref(sort, dir, "source_name", q, sharedParams)}>
                  来源
                </Link>
              </th>
              <th>
                <Link className="sort" href={sortHref(sort, dir, "title", q, sharedParams)}>
                  标题
                </Link>
              </th>
            </tr>
          </thead>
          <tbody>
            {pagedRows.length ? (
              pagedRows.map((item) => (
                <tr key={`${item.source_id}-${item.link}`}>
                  <td className="cell-time">{formatDate(item.display_time)}</td>
                  <td className="cell-ellipsis cell-muted" title={item.source_name}>
                    {item.source_name}
                  </td>
                  <td>
                    <a
                      className={`cell-ellipsis cell-link ${readLinks[item.link] ? "cell-read" : "cell-strong"}`}
                      href={item.link}
                      target="_blank"
                      rel="noreferrer"
                      title={item.title}
                      onClick={() => markRead(item.link)}
                    >
                      {item.title}
                    </a>
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
      <div className="pagination pagination-tight pagination-aligned">
        <div className="subtle">第 {safePage} / {totalPages} 页，共 {visibleRows.length} 条</div>
        <div className="toolbar">
          <button className={`btn ghost ${safePage <= 1 ? "disabled" : ""}`} type="button" onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={safePage <= 1}>
            上一页
          </button>
          <button className={`btn ghost ${safePage >= totalPages ? "disabled" : ""}`} type="button" onClick={() => setPage((current) => Math.min(totalPages, current + 1))} disabled={safePage >= totalPages}>
            下一页
          </button>
        </div>
      </div>
    </>
  );
}
