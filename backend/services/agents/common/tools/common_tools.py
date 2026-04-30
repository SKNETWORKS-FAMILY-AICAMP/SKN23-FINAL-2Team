"""
File    : backend/services/agents/common/tools/common_tools.py
Author  : 송주엽
Create  : 2026-04-16
Description : 전 도메인(건축/배관/전기/소방 등)이 공통으로 사용하는 LangChain @tool 함수 모음.
              각 도메인 에이전트는 이 파일에서 필요한 tool 을 import 하여
              자신의 TOOLS 리스트에 추가하면 됩니다.

              ── 사용법 (각 도메인 graph 파일) ──────────────────────────────
              from backend.services.agents.common.tools import (
                  search_law_tool,
                  get_cad_entity_info_tool,
              )

              # 도메인 전용 tool 과 합쳐서 바인딩
              TOOLS = [search_law_tool, get_cad_entity_info_tool, my_domain_tool]
              ────────────────────────────────────────────────────────────────

Modification History :
    - 2026-04-16 (송주엽) : 공통 tool 모듈 최초 작성
    - 2026-04-17 (송주엽) : common/tools/ 패키지로 구조 변경
    - 2026-04-17 (송주엽) : search_law_tool 실제 하이브리드 RAG 구현
                            (hybrid_search_permanent_chunks + hybrid_search_temp_chunks)
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# =========================================================
# 1. 법규 검색 Tool (도메인 공통)
# =========================================================
@tool
async def search_law_tool(
    query: str,
    domain: str = "",
    doc_name: str = "",
    spec_guid: str | None = None,
    org_id: str | None = None,
    limit: int = 5,
) -> str:
    """
    법규·기준 정보를 하이브리드 RAG(Dense + Sparse RRF)로 검색합니다.

    Args:
        query     : 검색 키워드 (예: "배관 경사 기준", "방화구획 면적")
        domain    : 도메인 필터 (예: "pipe", "arch", "elec", "fire").
                    ※ doc_name 을 지정한 경우 이 값은 무시됩니다.
                    사용자가 특정 문서를 언급하지 않았을 때만 현재 도메인을 지정하세요.
                    사용자가 다른 도메인 법규를 물어보면 해당 도메인으로 바꾸거나 빈 문자열로 두세요.
        doc_name  : 특정 문서명 또는 문서번호 필터 (부분 일치).
                    사용자가 특정 시방서·법규·문서번호를 언급한 경우 반드시 이 값을 채우세요.
                    doc_name 이 있으면 domain 필터는 자동으로 해제되어 전 도메인에서 해당 문서를 검색합니다.
                    예: "324010", "KEC 232", "소방시설 설치기준", "건축물방화구조규칙"
                    사용자가 특정 문서를 지정하지 않으면 빈 문자열("")로 두세요.
        spec_guid : 프로젝트 전용 임시 시방서 GUID (있으면 추가 검색)
        org_id    : 조직 ID (spec_guid 와 함께 사용)
        limit     : 검색 결과 최대 개수 (기본 5)

    Returns:
        검색된 법규 텍스트 (str). 영구 시방서 → 임시 시방서 순으로 반환.
        각 결과 앞에 [문서명·섹션ID] 접두사가 붙습니다.
    """
    from backend.core.database import SessionLocal
    from backend.services import vector_service

    db = SessionLocal()
    try:
        results: list[str] = []

        # doc_name 지정 시 domain 필터 해제 — 다른 도메인 에이전트에서도 해당 문서 접근 가능
        effective_domain = None if doc_name else (domain or None)

        # 1. 영구 시방서 검색 (국가 표준 / 관리자 업로드) - Reranker 적용
        perm_chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
            db,
            query,
            domain=effective_domain,
            doc_name=doc_name or None,
            final_limit=limit,
        )
        for chunk in perm_chunks:
            doc = getattr(chunk, "doc_name", None) or chunk.domain or domain or ""
            prefix = f"[{doc}]" if doc else "[영구]"
            if getattr(chunk, "section_id", None):
                prefix += f" {chunk.section_id}"
            results.append(f"{prefix} {chunk.content}")

        # 2. 임시 시방서 검색 (사용자 업로드, spec_guid + org_id 제공 시)
        if spec_guid and org_id:
            temp_chunks = await vector_service.hybrid_search_temp_chunks(
                db,
                query,
                spec_guid=spec_guid,
                org_id=org_id,
                limit=limit,
            )
            for chunk in temp_chunks:
                doc = getattr(chunk, "doc_name", None) or chunk.domain or domain
                prefix = f"[임시·{doc}]"
                if getattr(chunk, "section_id", None):
                    prefix += f" {chunk.section_id}"
                results.append(f"{prefix} {chunk.content}")

        if results:
            return "\n\n".join(results)

        hint = f"doc_name={doc_name!r}, " if doc_name else ""
        return f"[법규 검색 결과 없음 — {hint}query: {query!r}, domain: {domain!r}]"

    except Exception as exc:
        logger.error("[search_law_tool] RAG 검색 오류: %s", exc)
        return f"[오류] 법규 검색 중 문제가 발생했습니다: {exc}"
    finally:
        db.close()


# =========================================================
# 2. CAD 엔티티 정보 조회 Tool (도메인 공통)
# =========================================================
@tool
def get_cad_entity_info_tool(handle: str, drawing_data: str = "{}") -> str:
    """
    CAD 도면에서 특정 handle 의 엔티티 정보를 조회합니다.

    Args:
        handle       : 조회할 CAD 객체의 handle 값
        drawing_data : 도면 데이터 JSON 문자열
                       {"entities": [{handle, type, layer, ...}, ...]}

    Returns:
        엔티티 정보 JSON 문자열 (없으면 오류 메시지)
    """
    try:
        data: dict = json.loads(drawing_data) if isinstance(drawing_data, str) else drawing_data
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "drawing_data 파싱 실패"}, ensure_ascii=False)

    entities: list = data.get("entities") or data.get("elements") or []
    for ent in entities:
        if str(ent.get("handle", "")) == str(handle):
            return json.dumps(ent, ensure_ascii=False, indent=2)

    return json.dumps(
        {"error": f"handle={handle!r} 엔티티를 찾을 수 없습니다."},
        ensure_ascii=False,
    )


# =========================================================
# 3. 다중객체 매핑 모호성 해소 Tool (도메인 공통)
#    multi_object_mapper.py 의 llm_fallback_resolver 를 @tool 로 노출
# =========================================================
@tool
async def resolve_ambiguous_mapping_tool(
    text_entity_json: str,
    candidate_a_json: str,
    candidate_b_json: str,
    domain_hint: str = "",
) -> str:
    """
    두 후보 객체(candidate_a, candidate_b) 중 어느 쪽이 텍스트 엔티티와
    올바르게 매핑되는지 판단합니다.

    점수 차이가 작아 자동 매핑을 확신할 수 없을 때 호출하세요.

    Args:
        text_entity_json  : 텍스트 엔티티 JSON ({"handle":..., "text":..., ...})
        candidate_a_json  : 후보 A 엔티티 JSON
        candidate_b_json  : 후보 B 엔티티 JSON
        domain_hint       : 도메인 설명 (예: "배관", "건축") — 프롬프트 보강용

    Returns:
        선택된 후보의 handle 문자열
    """
    from backend.services.agents.common.multi_object_mapper import llm_fallback_resolver

    try:
        text_entity = json.loads(text_entity_json)
        candidate_a = json.loads(candidate_a_json)
        candidate_b = json.loads(candidate_b_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return f"ERROR: JSON 파싱 실패 — {exc}"

    try:
        return await llm_fallback_resolver(
            text_entity, candidate_a, candidate_b, domain_hint
        )
    except Exception as exc:
        logger.error("[resolve_ambiguous_mapping_tool] LLM fallback 실패: %s", exc)
        return candidate_a.get("handle", "UNKNOWN")


# =========================================================
# 편의 목록 — 도메인에서 그대로 import 해서 쓸 수 있게
# =========================================================
COMMON_TOOLS = [
    search_law_tool,
    get_cad_entity_info_tool,
    resolve_ambiguous_mapping_tool,
]
