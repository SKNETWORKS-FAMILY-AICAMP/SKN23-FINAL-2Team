/*
 * File        : frontend/src/api/agentApi.ts
 * Author      : 김지우
 * Create      : 2026-04-18
 * Description : Zustand(agentStore)에서 호출되어 에이전트의 실행을 트리거 및 결과 확정
 *
 * Modification History :
 *     - 2026-04-18 (김지우) : 초기 API 구조 생성
 *     - 2026-04-19 (김지우) : 백엔드 snake_case 스키마 대응을 위한 페이로드 필드 매핑 로직 추가
 */

import axios from "axios";
import { getAgentApiHeaders } from "../utils/documentApiAuth";

// ----------------------------------------------------------------------
// 1. Type Definitions (API 규격)
// ----------------------------------------------------------------------

export interface AgentReviewRequest {
  sessionId: string;
  cadCacheId?: string;
  domain: string;
  activeObjectIds: string[];
  specDocumentIds?: string[];
  tempSpecIds?: string[];
  reviewMode: "KEC_ONLY" | "HYBRID";
  userPrompt?: string;
  orgId?: string;
}

export interface AgentReviewResponse {
  status: "SUCCESS" | "ERROR" | "DOMAIN_MISMATCH";
  jobId: string;
  message: string;
}

export interface ChecklistRequest {
  specDocumentIds: string[];
}

/** 수정 확정 요청 페이로드 */
export interface FixConfirmRequest {
  session_id: string;
  selected_fix_ids: string[];
}

// ----------------------------------------------------------------------
// 2. API Functions
// ----------------------------------------------------------------------

const BASE_URL =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(
    /\/$/,
    "",
  ) || "http://localhost:8000") + "/api/v1/agent";

export const agentApi = {
  /** AI 에이전트에게 도면 검토 작업을 요청 */
  startReview: async (
    payload: AgentReviewRequest,
  ): Promise<AgentReviewResponse> => {
    try {
      const orgId = payload.orgId || localStorage.getItem("skn23_org_id") || "";
      const machineId = localStorage.getItem("skn23_machine_id") || "";
      const serverPayload = {
        session_id: payload.sessionId,
        cad_cache_id: payload.cadCacheId || payload.sessionId,
        domain: payload.domain,
        active_object_ids: payload.activeObjectIds,
        spec_document_ids: payload.specDocumentIds,
        temp_spec_ids: payload.tempSpecIds,
        review_mode: payload.reviewMode,
        user_prompt: payload.userPrompt,
        org_id: orgId,
        machine_id: machineId,
        skip_classification: (payload as any).skipClassification || false,
      };
      const response = await axios.post<AgentReviewResponse>(
        `${BASE_URL}/start`,
        serverPayload,
        {
          headers: { ...getAgentApiHeaders() },
        },
      );
      return response.data;
    } catch (error) {
      console.error("에이전트 실행 요청 실패:", error);
      throw error;
    }
  },

  /** 도메인 불일치 경고 후 사용자가 계속 진행을 선택했을 때 — skip_classification=true */
  confirmReview: async (
    payload: AgentReviewRequest,
  ): Promise<AgentReviewResponse> => {
    return agentApi.startReview({
      ...payload,
      skipClassification: true,
    } as any);
  },

  /** 사용자가 승인한 수정 사항을 DB에 영구 저장 */
  confirmFixes: async (payload: FixConfirmRequest): Promise<any> => {
    try {
      const response = await axios.post(`${BASE_URL}/fixes/confirm`, payload, {
        headers: { ...getAgentApiHeaders() },
      });
      return response.data;
    } catch (error) {
      console.error("수정 상태 DB 저장 실패:", error);
      throw error;
    }
  },

  /** 에이전트 실행 강제 중단 */
  cancelReview: async (jobId: string): Promise<void> => {
    try {
      await axios.post(`${BASE_URL}/cancel/${jobId}`, undefined, {
        headers: { ...getAgentApiHeaders() },
      });
    } catch (error) {
      console.error("에이전트 실행 취소 실패:", error);
      throw error;
    }
  },

  /** 시방서 기반 AI 체크리스트 자동 생성 */
  generateChecklist: async (payload: ChecklistRequest): Promise<any> => {
    try {
      // XCUT-7: camelCase → snake_case 변환 (백엔드 ChecklistRequest.spec_document_ids)
      const serverPayload = {
        spec_document_ids: payload.specDocumentIds,
      };
      const response = await axios.post(
        `${BASE_URL}/checklist/generate`,
        serverPayload,
        {
          headers: { ...getAgentApiHeaders() },
        },
      );
      return response.data;
    } catch (error) {
      console.error("체크리스트 생성 요청 실패:", error);
      throw error;
    }
  },
};
