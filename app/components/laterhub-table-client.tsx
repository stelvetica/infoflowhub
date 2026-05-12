"use client";

import { useEffect, useMemo, useState } from "react";
import { finishLaterhubAction } from "@/app/actions";
import { SubmitButton } from "@/app/components/submit-button";
import { formatDate } from "@/lib/utils";

type LaterhubRow = {
  id: number;
  url: string;
  title: string;
  is_finished: number;
  created_at: string;
  tags_text: string;
};

type LaterhubTableClientProps = {
  rows: LaterhubRow[];
};

const PAGE_SIZE = 18;

export function LaterhubTableClient({ rows }: LaterhubTableClientProps) {
  const [page, setPage] = useState(1);

  useEffect(() => {
    setPage(1);
  }, [rows]);

  const totalPages = Math.max(Math.ceil(rows.length / PAGE_SIZE), 1);
  const safePage = Math.min(page, totalPages);
  const visibleRows = useMemo(() => rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE), [rows, safePage]);

  return (
    <div className="laterhub-client-wrap">
      <div className="table-wrap">
        <table className="laterhub-table">
          <colgroup>
            <col className="col-time" />
            <col className="col-title" />
            <col className="col-action" />
          </colgroup>
          <thead>
            <tr>
              <th>时间</th>
              <th>链接</th>
              <th>完成</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.length ? (
              visibleRows.map((item) => (
                <tr key={item.id}>
                  <td className="cell-time cell-time-small">{formatDate(item.created_at)}</td>
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
                  <td className="laterhub-action-cell laterhub-action-cell-small">
                    <form action={finishLaterhubAction}>
                      <input type="hidden" name="id" value={item.id} />
                      <input type="hidden" name="finished" value={item.is_finished ? "0" : "1"} />
                      <SubmitButton className={`btn ${item.is_finished ? "secondary" : ""}`} idleText={item.is_finished ? "未完成" : "完成"} pendingText="提交中..." />
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
      <div className="pagination pagination-tight pagination-aligned">
        <div className="subtle">
          第 {safePage} / {totalPages} 页，共 {rows.length} 条
        </div>
        <div className="toolbar">
          <button className={`btn ghost ${safePage <= 1 ? "disabled" : ""}`} type="button" onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={safePage <= 1}>
            上一页
          </button>
          <button className={`btn ghost ${safePage >= totalPages ? "disabled" : ""}`} type="button" onClick={() => setPage((current) => Math.min(totalPages, current + 1))} disabled={safePage >= totalPages}>
            下一页
          </button>
        </div>
      </div>
    </div>
  );
}
