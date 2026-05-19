// frontend/src/components/DrawingStatusBadge.tsx
import React from "react";
import { useAgentStore } from "../store/agentStore";
import type { DrawingStatus } from "../store/agentStore";

const STATUS_CONFIG: Record<DrawingStatus, { label: string; colorClass: string }> = {
  LATEST:              { label: "최신",              colorClass: "text-green-500"  },
  DIRTY:               { label: "변경됨",            colorClass: "text-yellow-500" },
  INCREMENTAL_PENDING: { label: "변경 감지 중",      colorClass: "text-yellow-500" },
  INCREMENTAL_READY:   { label: "부분 갱신 완료",    colorClass: "text-orange-500" },
  FULL_RESYNC_PENDING: { label: "전체 재동기화 필요", colorClass: "text-orange-500" },
  FULL_RESYNC_RUNNING: { label: "전체 재동기화 중",  colorClass: "text-gray-400"   },
  RESYNC_FAILED:       { label: "재동기화 실패",     colorClass: "text-red-500"    },
  REVIEW_REQUIRED:     { label: "검토 필요",         colorClass: "text-orange-500" },
};

export function isReviewButtonDisabled(status: DrawingStatus, canReviewStale: boolean): boolean {
  if (status === "INCREMENTAL_PENDING") return true;
  if (status === "FULL_RESYNC_RUNNING") return true;
  if (status === "FULL_RESYNC_PENDING") return true;
  if (status === "RESYNC_FAILED" && !canReviewStale) return true;
  return false;
}

export function DrawingStatusBadge() {
  const drawingState = useAgentStore((s) => s.drawingState);
  if (!drawingState) return null;

  const config = STATUS_CONFIG[drawingState.status] ?? {
    label: drawingState.status,
    colorClass: "text-gray-400",
  };

  return (
    <div className={`flex items-center gap-1 text-xs font-medium ${config.colorClass}`}>
      <span className="w-2 h-2 rounded-full bg-current" />
      <span>{config.label}</span>
      {drawingState.status === "RESYNC_FAILED" && drawingState.canReviewStaleSnapshot && (
        <span className="text-gray-400 ml-1">(기존 snapshot 기준 검토 가능)</span>
      )}
      {drawingState.dirtyCount > 0 && (
        <span className="text-gray-400 ml-1">dirty {drawingState.dirtyCount}개</span>
      )}
      {drawingState.deltaCount > 0 && (
        <span className="text-gray-400 ml-1">delta {drawingState.deltaCount}개</span>
      )}
    </div>
  );
}
