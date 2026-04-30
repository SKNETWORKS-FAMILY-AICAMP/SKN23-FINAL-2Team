/*
 * File    : frontend/src/types/spec.ts
 * Author      : 김지우
 * Create      : 2026-04-06
 * Description : 시방서(temp_documents) 관련 타입 정의
 *
 * Modification History :
 *   - 2026-04-06 (김지우) : 초기 구조 생성
 */

export interface SpecDocument {
  id: string;
  project_id: string;
  session_id: string;
  file_name: string;
  storage_type: string;
  storage_url: string;
  comment: string;
  status: string;
  expires_at: string;
  created_at: string;
}
