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
  setDomainMismatch: (info: DomainMismatchInfo) => void;
  clearDomainMismatch: () => void;
  clearCadUnmapped: () => void;
  clearResults: () => void;
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
        progressLogs: [...state.progressLogs, payload.message],
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
}));

export default useAgentStore;
