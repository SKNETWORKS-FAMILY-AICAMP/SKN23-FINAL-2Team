/*
 * File    : frontend/src/api/specApi.ts
 * Author      : 김지우
 * Create      : 2026-04-06
 * Description : 시방서(temp_documents) 관련 API 호출부
 *              - 시방서 업로드, 목록 조회, 삭제 관련 Axios 로직
 *
 * Modification History :
 *   - 2026-04-06 (김지우) : 초기 구조 생성 및 FastAPI 연동
 */

import axios from "axios";
import { SpecDocument } from "../types/spec";
import { getDocumentApiHeaders } from "../utils/documentApiAuth";

const API_BASE =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
    "http://localhost:8000") + "/api/v1/docs";

export const specApi = {
  // 목록 조회
  fetchList: (projectId: string) =>
    axios.get<SpecDocument[]>(`${API_BASE}/list?project_id=${projectId}`, {
      headers: { ...getDocumentApiHeaders() },
    }),

  // 업로드
  upload: (formData: FormData) =>
    axios.post(`${API_BASE}/upload/temp`, formData, {
      headers: { "Content-Type": "multipart/form-data", ...getDocumentApiHeaders() },
    }),

  // 삭제
  delete: (ids: string[]) =>
    axios.post(`${API_BASE}/delete`, { ids }, {
      headers: { ...getDocumentApiHeaders() },
    }),
};
