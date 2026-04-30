"""
File    : backend/services/agents/common/tools/elec_tools.py
Author  : 김지우
Create  : 2026-04-17
Description : 전기 도메인 특화 수치 연산 및 규정 검증 도구 모음 (LangChain @tool)
              임시 및 영구 시방서를 통합 검색하는 전기 특화 도구 포함

Modification History :
    - 2026-04-17 (김지우) : 국가 표준 + 임시 시방서 통합 검색(Dual-Search) 로직 반영
    - 2026-04-17 (송주엽) : common/tools/ 패키지로 경로 이동
    - 2026-04-22 (김지우) : tools 내용 추가
"""
import math
import asyncio
from typing import List

from langchain_core.tools import tool

from backend.services.vector_service import (
    hybrid_search_permanent_chunks_with_rerank,
    hybrid_search_temp_chunks_with_rerank,
)
from backend.core.database import get_db as get_db_session


@tool
def calculate_voltage_drop_tool(length_m: float, current_a: float, area_sqmm: float, phase: str = "1") -> str:
    """
    단상 또는 3상 회로의 전압 강하(e)를 계산합니다.
    Args:
        length_m: 전선 길이 (m)
        current_a: 부하 전류 (A)
        area_sqmm: 전선 단면적 (SQ, mm^2)
        phase: 상 방식 ("1": 단상 2선식, "3": 3상 3선식/4선식)
    """
    try:
        coef = 35.6 if phase == "1" else 30.8
        voltage_drop = (coef * float(length_m) * float(current_a)) / (1000 * float(area_sqmm))
        return f"전압강하: {voltage_drop:.2f}V (계수: {coef})"
    except Exception as e:
        return f"계산 오류: {str(e)}"


@tool
def check_clearance_distance_tool(x1: float, y1: float, x2: float, y2: float, required_mm: float) -> str:
    """도면 상 두 객체 간의 2D 이격 거리를 계산하고 규정 위반 여부를 판별합니다."""
    actual_dist = math.hypot(x1 - x2, y1 - y2)
    status = "PASS" if actual_dist >= required_mm else "FAIL"
    return f"실측 이격거리: {actual_dist:.1f}mm / 기준: {required_mm}mm -> 판정: [{status}]"


@tool
def calculate_cable_ampacity_tool(load_watt: float, voltage: float = 220.0, power_factor: float = 0.9) -> str:
    """설비 부하 용량을 바탕으로 회로의 예상 전류(A)를 산출합니다."""
    try:
        current = load_watt / (voltage * power_factor)
        return f"산출 전류: {current:.2f}A (역률 {power_factor})"
    except ZeroDivisionError:
        return "계산 오류: 전압이나 역률이 0입니다."


@tool
def calculate_breaker_capacity_tool(design_current_a: float) -> str:
    """
    산출된 부하 전류를 바탕으로 적절한 차단기 정격전류(AT)를 산정합니다.
    KEC 규정에 따른 여유율(통상 1.25배)을 적용한 권장 값을 반환합니다.
    """
    try:
        margin_current = design_current_a * 1.25
        # 상용 차단기 AT 규격 리스트
        standard_breakers = [15, 20, 30, 40, 50, 75, 100, 125, 150, 200, 250, 300, 400]
        
        recommended_at = next((at for at in standard_breakers if at >= margin_current), None)
        
        if recommended_at:
            return f"설계 전류: {design_current_a}A -> 권장 차단기 용량: {recommended_at}AT (여유율 반영)"
        else:
            return f"설계 전류: {design_current_a}A -> 상용 규격(400AT) 초과. 별도 정밀 검토 필요."
    except Exception as e:
        return f"차단기 산정 오류: {str(e)}"


@tool
async def search_electric_law_tool(query: str, spec_guid: str = None, org_id: str = None) -> str:
    """KEC 국가 표준과 프로젝트별 임시 시방서를 통합 검색합니다."""
    async for db in get_db_session():
        try:
            tasks = []
            
            # 국가 표준 검색 (항상 포함)
            tasks.append(hybrid_search_permanent_chunks_with_rerank(
                db=db, query=query, domain="전기", final_limit=3
            ))

            # 회사/프로젝트 ID가 유효한 경우에만 임시 시방서 검색 추가
            search_temp = False
            if spec_guid and org_id:
                search_temp = True
                tasks.append(hybrid_search_temp_chunks_with_rerank(
                    db=db, query=query, spec_guid=spec_guid, org_id=org_id, final_limit=3
                ))

            results = await asyncio.gather(*tasks)
            permanent_results = results[0]
            temp_results = results[1] if search_temp else []

            context = []

            # 1. 임시 시방서가 존재하면 최상단에 배치하여 최우선 참조 유도
            if temp_results:
                context.append("[우선 참조: 해당 프로젝트/사내 전용 시방서]")
                context.extend([f"- {r.content}" for r in temp_results])
                context.append("") # 섹션 구분을 위한 개행

            # 2. 국가 표준은 항상 제공하거나 임시 지침이 없을 때의 보완 근거로 사용
            if permanent_results:
                context.append("[참조: 국가 표준 전기설비규정 (KEC)]")
                context.extend([f"- {r.content}" for r in permanent_results])

            if not context:
                return "관련 규정이나 시방서 내용을 찾을 수 없습니다."

            return "\n".join(context)

        except Exception as e:
            return f"검색 중 오류 발생: {str(e)}"


@tool
def calculate_required_luminaires_tool(room_area_sqm: float, target_lux: float, lumen_per_fixture: float, utilization_factor: float = 0.5, maintenance_factor: float = 0.8) -> str:
    """
    실의 면적과 목표 조도를 바탕으로 필요한 조명기구의 최소 개수를 계산합니다. (광속법 적용)
    
    Args:
        room_area_sqm: 대상 공간의 면적 (m^2)
        target_lux: KEC 또는 설계 기준 목표 조도 (lx)
        lumen_per_fixture: 조명 기구 1개당 광속 (lm)
        utilization_factor: 조명률 (기본 0.5)
        maintenance_factor: 유지보수율/보상률 (기본 0.8)
    """
    try:
        # N = (E * A) / (F * U * M)
        required_count = (target_lux * room_area_sqm) / (lumen_per_fixture * utilization_factor * maintenance_factor)
        return f"면적 {room_area_sqm}m^2, 목표 {target_lux}lx 기준 -> 최소 필요 조명 수량: {math.ceil(required_count)}개"
    except Exception as e:
        return f"조명 수량 산정 오류: {str(e)}"

@tool
def calculate_conduit_size_tool(wire_outer_areas: List[float], max_fill_ratio: float = 0.32) -> str:
    """
    전선들의 절연체를 포함한 단면적 합계를 바탕으로 KEC 점적률 기준을 만족하는 최소 전선관 내경 단면적을 산출합니다.
    
    Args:
        wire_outer_areas: 관 내부에 입선되는 각 전선들의 외경 기준 단면적 리스트 (mm^2)
        max_fill_ratio: 허용 점적률 (서로 다른 굵기 0.32, 같은 굵기 0.48 적용)
    """
    try:
        total_wire_area = sum(wire_outer_areas)
        min_conduit_area = total_wire_area / max_fill_ratio
        
        # 상용 후강전선관(스틸) 내경 단면적 근사치 리스트 (16C, 22C, 28C, 36C, 42C, 54C...)
        standard_conduits = {
            16: 201, 22: 380, 28: 615, 36: 1017, 42: 1385, 54: 2290
        }
        
        recommended_size = next((size for size, area in standard_conduits.items() if area >= min_conduit_area), None)
        
        if recommended_size:
            return f"전선 단면적 합: {total_wire_area:.1f}mm^2 -> 권장 전선관: {recommended_size}C (점적률 {max_fill_ratio*100}% 적용)"
        else:
            return f"필요 배관 단면적({min_conduit_area:.1f}mm^2)이 상용 규격(54C)을 초과합니다. 배관 분리 검토 요망."
    except Exception as e:
        return f"전선관 산정 오류: {str(e)}"

@tool
def calculate_grounding_wire_tool(phase_wire_sqmm: float) -> str:
    """
    KEC 규정에 따라 상도체(전원선)의 단면적을 기준으로 적절한 보호도체(접지선)의 최소 단면적을 산출합니다.
    """
    try:
        if phase_wire_sqmm <= 16:
            ground_wire = phase_wire_sqmm
        elif 16 < phase_wire_sqmm <= 35:
            ground_wire = 16.0
        else:
            ground_wire = phase_wire_sqmm / 2.0
            
        return f"상도체 굵기 {phase_wire_sqmm}sq 기준 -> 최소 접지선 굵기: {ground_wire}sq"
    except Exception as e:
        return f"접지선 산정 오류: {str(e)}"

@tool
def calculate_demand_load_tool(connected_loads_kw: List[float], demand_factor: float) -> str:
    """
    분전반 하위에 연결된 부하들의 합산 용량에 수용률을 적용하여 최종 설계 부하(수용 부하)를 산출합니다.
    
    Args:
        connected_loads_kw: 연결된 각 회로의 부하 용량 리스트 (kW)
        demand_factor: 수용률 (0.0 ~ 1.0)
    """
    try:
        total_connected = sum(connected_loads_kw)
        demand_load = total_connected * demand_factor
        return f"총 접속부하: {total_connected:.2f}kW, 수용률: {demand_factor} -> 설계(수용) 부하: {demand_load:.2f}kW"
    except Exception as e:
        return f"부하 산정 오류: {str(e)}"

ELECTRIC_TOOLS = [
    search_electric_law_tool,
    calculate_voltage_drop_tool,
    check_clearance_distance_tool,
    calculate_cable_ampacity_tool,
    calculate_breaker_capacity_tool,
    calculate_required_luminaires_tool,
    calculate_conduit_size_tool,
    calculate_grounding_wire_tool,
    calculate_demand_load_tool
]
