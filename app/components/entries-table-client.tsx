"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

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
};

const STORAGE_KEY = "infoflowhub:read-links";

function sortHref(currentSort: string, currentDir: string, nextSort: string, q: string, time = false) {
  const nextDir = time
    ? currentSort !== nextSort || currentDir === "asc"
      ? "desc"
      : "asc"
    : currentSort !== nextSort || currentDir === "desc"
      ? "asc"
      : "desc";
  const search = new URLSearchParams({ view: "entries", sort: nextSort, dir: nextDir });
  if (q) search.set("q", q);
  return `/?${search.toString()}`;
}

export function EntriesTableClient({ rows, sort, dir, q }: EntriesTableClientProps) {
  const [showUnreadOnly, setShowUnreadOnly] = useState(false);
  const [readLinks, setReadLinks] = useState<Record<string, boolean>>({});

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      setReadLinks(JSON.parse(raw) as Record<string, boolean>);
    } catch {
      setReadLinks({});
    }
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

  return (
    <>
      <div className="panel-title">
        <div />
        <button className={`btn ghost ${showUnreadOnly ? "active-filter" : ""}`} type="button" onClick={() => setShowUnreadOnly((current) => !current)}>
          {showUnreadOnly ? "显示全部" : "未读"}
        </button>
      </div>
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
                <Link className="sort" href={sortHref(sort, dir, "sort_time", q, true)}>
                  时间
                </Link>
              </th>
              <th>
                <Link className="sort" href={sortHref(sort, dir, "source_name", q)}>
                  来源
                </Link>
              </th>
              <th>
                <Link className="sort" href={sortHref(sort, dir, "title", q)}>
                  标题
                </Link>
              </th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.length ? (
              visibleRows.map((item) => (
                <tr key={`${item.source_id}-${item.link}`}>
                  <td className="cell-time">{item.display_time}</td>
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
    </>
  );
}
