"""
에이전트(배관/타 도메인) 런타임이 사용하는 DB 식별 — 조직(결제·라이선스) 테이블은 제외.

| 테이블            | 용도 |
|-------------------|------|
| chat_sessions     | 세션 id, domain_type, summary_text, recent_chat (롤링 메모리) |
| chat_messages     | 턴 로그. append_chat_message: role, content, tool_calls, active_object_ids |
| review_results     | HITL pending_fixes, reference_chunk_id → document_chunks |
| documents_s3      | 영구 시방/표준 문서 메타 + document_chunks RAG |
| document_chunks   | RAG, domain(예: pipe) |
| temp_documents / temp_document_chunks | 사용자 업로드 임시 시방 (org_id 있음) |
| standard_terms    | 용어·매핑 (도메인별) |
| mapping_rules     | org별 스타일 매핑(배관). layer_role(arch/mep/aux) — POST /cad/mapping-rules/layer-batch 의 layer_role 필드로 저장·캐시 무효화 |

-- 아래 chat_sessions / chat_messages 컬럼은 ORM/마이그레이션에만 있고, 현재 Python 코드에서 쓰지 않음(예약):
   chat_sessions.langgraph_thread_id, chat_sessions.expires_at
   chat_messages.agent_name, chat_messages.tool_call_id,
   chat_messages.approval_status, chat_messages.metadata
"""
