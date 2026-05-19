/*
File : front/src/store/agentStore.ts
Author : 김지우
Create : 2026-03-31
Modified: 2026-04-18 (객체 선택 정보 상태 추가)
         2026-04-23 (김다빈) domainMismatch 상태 추가 (도메인 분류기 불일치 감지)
Description : Zustand를 사용해 에이전트 대화 내역, AI 검토 결과 및 현재 선택된 도면 객체를 전역으로 관리
*/

import { create } from "zustand";

export interface MessageSource {
  title: string;
  url?: string;
}

export interface SuggestedQuery {
  label: string;
  query: string;
}

export interface ResponseMeta {
  answer_type?: string;
  used_rag?: boolean;
  suggested_queries?: SuggestedQuery[];
  [key: string]: unknown;
}

export interface Message {
  id: string;
  sender: "user" | "agent" | "system";
  text: string;
  /** 말풍선 시각(ms) — id가 DB UUID일 때 */
  ts?: number;
  workedTime?: string;
  timestamp?: string;                  // ISO 8601 — 표시용 시간
  sources?: MessageSource[];           // RAG 출처 (있을 때만 표시)
  streaming?: boolean;                 // true이면 ChatBubble이 타이핑 효과 적용
  thinkingLogs?: string[];             // 분석 과정 로그 (영구 보관용)
  thinkingTime?: number;               // 분석에 걸린 시간 (초)
  response_meta?: ResponseMeta;        // 백엔드 response_meta (suggested_queries 등)
  domain?: string;                     // 메시지 발신 시 도메인 ("elec" 등)
}

export interface Violation {
  id: string;
  type?: string;
  violation_type?: string;
  source?: string;
  _source?: string;
  confidence_score?: number | null;
  confidence_reason?: string | null;
  rule?: string;
  severity?: "Critical" | "Major" | "Minor";
  description: string;
  suggestion?: string;
  auto_fix?: any;
  proposed_action?: any;
  /** 백엔드/C# 내부 분류(필요 시) */
  modification_tier?: number;
}

export interface AnnotatedEntity {
  handle: string;
  type?: string;
  layer?: string;
  /** RevCloud·줌인용 도면 좌표 (백엔드 annotated_entities) */
  bbox?: { x1: number; y1: number; x2: number; y2: number };
  violation?: Violation;
}

export interface CreationProposal {
  proposal_id: string;
  count: number;
  handles: string[];
  layers: string[];
  description: string;
  delete_count?: number;
  proposal_type?: "create" | "replace" | "modify";
}

export interface DomainMismatchInfo {
  predicted_domain: string;
  predicted_domain_kr: string;
  original_domain: string;
  original_domain_kr: string;
  probabilities: Record<string, number>;
  session_id: string;
  message: string;
}

/** 'full' = 도면 전체 추출, 'focus' = 선택 객체 부분 추출 */
export type ExtractionMode = "full" | "focus";

export type DrawingStatus =
  | "LATEST" | "DIRTY" | "INCREMENTAL_PENDING" | "INCREMENTAL_READY"
  | "FULL_RESYNC_PENDING" | "FULL_RESYNC_RUNNING" | "RESYNC_FAILED"
  | "REVIEW_REQUIRED";

export type DrawingChangeMode =
  | "NONE" | "INCREMENTAL_PENDING" | "INCREMENTAL_READY"
  | "FULL_RESYNC_PENDING" | "FULL_RESYNC_RUNNING";

export interface DrawingState {
  drawingId:              string;
  status:                 DrawingStatus;
  changeMode:             DrawingChangeMode;
  dirtyCount:             number;
  deltaCount:             number;
  deltaSize:              number;
  currentSnapshotId:      string | null;
  currentSnapshotPath:    string | null;
  latestDeltaPath:        string | null;
  pendingAfterResync:     boolean;
  canReviewStaleSnapshot: boolean;
  lastError:              string | null;
}

interface AgentState {
  messages: Message[];
  isAnalyzing: boolean;
  /** 마지막 단계 설명(실시간 경과는 UI에서 now-anchor 로 계산) */
  progressLine: {
    message: string;
    stage?: string;
    stepMs: number;
    totalMs: number;
  } | null;
  progressLogs: string[];
  reviewResults: AnnotatedEntity[];
  activeObjectIds: string[];
  selectedSpecIds: string[];
  selectedTempSpecIds: string[];
  /** 'full': 전체 도면 검토, 'focus': 선택 객체 부분 검토 */
  extractionMode: ExtractionMode;
  /** CAD_DATA_READY / CAD_ANALYZE_COMPLETE — unmappedLayers는 배관 매핑 미해결 레이어 목록 */
  cadEvent: { sessionId: string; ts: number; unmappedLayers?: string[] } | null;
  domainMismatch: DomainMismatchInfo | null;
  /** CAD_DATA_READY에서 수신한 파일 변경 감지용 fingerprint (없으면 null) */
  fileFingerprint: string | null;

  addMessage: (msg: Message) => void;
  setMessages: (msgs: Message[]) => void;
  setAnalyzing: (status: boolean) => void;
  setPipelineProgress: (payload: {
    pipelineStartedAtIso?: string;
    message: string;
    stage?: string;
    stepMs: number;
    totalMs: number;
  } | null) => void;
  setReviewResults: (data: { annotated_entities: AnnotatedEntity[] }) => void;
  setActiveObjectIds: (ids: string[], mode?: ExtractionMode) => void;
  setSelectedSpecIds: (ids: string[]) => void;
  setSelectedTempSpecIds: (ids: string[]) => void;
  setCadSessionId: (sessionId: string, unmappedLayers?: string[]) => void;
  clearCadSessionId: () => void;
  setDomainMismatch: (info: DomainMismatchInfo) => void;
  clearDomainMismatch: () => void;
  clearCadUnmapped: () => void;
  clearResults: () => void;
  drawingState: DrawingState | null;
  setDrawingState: (state: DrawingState) => void;
  clearDrawingState: () => void;
  setFileFingerprint: (fp: string | null) => void;
  pendingCreationProposals: CreationProposal[];
  addCreationProposal: (p: CreationProposal) => void;
  removeCreationProposal: (proposalId: string) => void;
}

export const useAgentStore = create<AgentState>((set) => ({
  messages: [],
  isAnalyzing: false,
  progressLine: null,
  progressLogs: [],
  reviewResults: [],
  activeObjectIds: [],
  selectedSpecIds: [],
  selectedTempSpecIds: [],
  extractionMode: "full",
  cadEvent: null,
  domainMismatch: null,
  fileFingerprint: null,
  drawingState: null,
  pendingCreationProposals: [],

  addMessage: (msg) => set((state) => ({ messages: [...state.messages, msg] })),
  setMessages: (msgs) => set({ messages: msgs }),
  setAnalyzing: (status) =>
    set({
      isAnalyzing: status,
      ...(status === false
        ? {
          progressLine: null,
          progressLogs: [],
        }
        : {}),
    }),
  setPipelineProgress: (payload) =>
    set((state) => {
      if (payload == null) {
        return { progressLine: null, progressLogs: [] };
      }
      const st = payload.stage;
      const stageClean =
        st && !String(st).startsWith("__") ? String(st) : undefined;
      return {
        progressLine: {
          message: payload.message,
          stage: stageClean,
          stepMs: payload.stepMs,
          totalMs: payload.totalMs,
        },
        progressLogs: [...state.progressLogs, payload.message].slice(-5),
      };
    }),
  setReviewResults: (data) =>
    set({
      reviewResults: data.annotated_entities || [],
      isAnalyzing: false,
      progressLine: null,
      progressLogs: [],
    }),
  setActiveObjectIds: (ids, mode) =>
    set({ activeObjectIds: ids, ...(mode != null ? { extractionMode: mode } : {}) }),
  setSelectedSpecIds: (ids) => set({ selectedSpecIds: ids }),
  setSelectedTempSpecIds: (ids) => set({ selectedTempSpecIds: ids }),
  setCadSessionId: (sessionId, unmappedLayers) =>
    set({
      cadEvent: {
        sessionId,
        ts: Date.now(),
        ...(unmappedLayers != null && unmappedLayers.length > 0
          ? { unmappedLayers: [...unmappedLayers] }
          : {}),
      },
    }),
  clearCadSessionId: () => set({ cadEvent: null }),
  setDomainMismatch: (info) => set({ domainMismatch: info, isAnalyzing: false }),
  clearDomainMismatch: () => set({ domainMismatch: null }),
  clearCadUnmapped: () =>
    set((s) => ({
      cadEvent: s.cadEvent
        ? { ...s.cadEvent, unmappedLayers: [] }
        : null,
    })),
  clearResults: () =>
    set({
      reviewResults: [],
      isAnalyzing: false,
      activeObjectIds: [],
      domainMismatch: null,
      extractionMode: "full",
      progressLine: null,
      progressLogs: [],
    }),
  setDrawingState: (state) => set({ drawingState: state }),
  clearDrawingState: () => set({ drawingState: null }),
  setFileFingerprint: (fp) => set({ fileFingerprint: fp }),
  addCreationProposal: (p) =>
    set((s) => ({
      pendingCreationProposals: [
        ...s.pendingCreationProposals.filter((x) => x.proposal_id !== p.proposal_id),
        p,
      ],
    })),
  removeCreationProposal: (proposalId) =>
    set((s) => ({
      pendingCreationProposals: s.pendingCreationProposals.filter(
        (x) => x.proposal_id !== proposalId,
      ),
    })),
}));

export default useAgentStore;
