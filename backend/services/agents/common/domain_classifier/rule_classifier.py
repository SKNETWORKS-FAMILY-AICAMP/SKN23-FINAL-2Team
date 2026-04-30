"""
File    : backend/services/agents/common/domain_classifier/rule_classifier.py
Author  : 김다빈
Create  : 2026-04-26

Description :
    CAD 블록 시그니처 기반 규칙 선처리 분류기.
    소방·전기·배관 도면의 블록 이름 패턴을 매칭하여 확실한 케이스를 100% 정확도로 처리.
    불명확한 경우 None을 반환하여 ML(XGBoost) 분류기로 위임.

    arch는 규칙 선처리 대상 아님 — 건축 도면은 블록 시그니처가 없어
    구조 기반 판단이 ML보다 열등하므로 무조건 ML로 분류.

    판정 로직:
        1. INSERT/BLOCK 타입 엔티티에서 block_name / standard_name 추출
        2. 도메인별 키워드와 대소문자 무관 포함 매칭
        3. 전체 블록 대비 매칭 비율이 임계값(15%) 이상이고
           다른 도메인보다 우세하면 해당 도메인 확정
        4. 조건 미충족 시 None 반환 → ML로 위임

Modification History :
    2026-04-26 | 김다빈 | 최초 작성
"""
from __future__ import annotations


class RuleClassifier:
    """
    CAD 시그니처 기반 선처리 분류기.
    명확한 케이스는 도메인 문자열 반환, 불명확한 케이스는 None 반환 → ML로 위임.
    """

    _FIRE_BLOCK_KEYWORDS = (
        "감지기", "DETECTOR",
        "스프링클러", "SPRINKLER",
        "소화기", "EXTINGUISHER",
        "소화전", "HYDRANT",
        "방화댐퍼", "FD",
        "연기", "SMOKE",
    )
    _ELEC_BLOCK_KEYWORDS = (
        "분전반", "PANEL",
        "차단기", "BREAKER",
        "콘센트", "OUTLET",
        "스위치", "SWITCH",
        "전등", "LAMP",
        "형광등", "조명",
    )
    _PIPE_BLOCK_KEYWORDS = (
        "밸브", "VALVE",
        "펌프", "PUMP",
        "배관", "PIPE",
        "플랜지", "FLANGE",
        "트랩", "TRAP",
        "스트레이너",
    )

    _THRESHOLD = 0.15  # 전체 블록 대비 최소 매칭 비율

    def predict(self, cad_json: dict) -> str | None:
        """
        명확한 케이스 → 도메인 문자열("fire" | "elec" | "pipe"),
        불명확 또는 arch → None

        Parameters
        ----------
        cad_json : dict
            drawing_unit, layers[], entities[] (또는 elements[]) 포함

        Returns
        -------
        str | None
        """
        elements = cad_json.get("entities") or cad_json.get("elements", [])

        block_names = [
            str(e.get("block_name") or e.get("standard_name") or "").upper()
            for e in elements
            if str(e.get("raw_type") or e.get("type") or "").upper() in ("INSERT", "BLOCK")
        ]

        if not block_names:
            return None

        total_blocks = len(block_names)

        fire_hits = sum(
            1 for b in block_names
            if any(k.upper() in b for k in self._FIRE_BLOCK_KEYWORDS)
        )
        elec_hits = sum(
            1 for b in block_names
            if any(k.upper() in b for k in self._ELEC_BLOCK_KEYWORDS)
        )
        pipe_hits = sum(
            1 for b in block_names
            if any(k.upper() in b for k in self._PIPE_BLOCK_KEYWORDS)
        )

        fire_ratio = fire_hits / total_blocks
        elec_ratio = elec_hits / total_blocks
        pipe_ratio = pipe_hits / total_blocks

        if (fire_ratio >= self._THRESHOLD
                and fire_hits > elec_hits
                and fire_hits > pipe_hits):
            return "fire"
        if (elec_ratio >= self._THRESHOLD
                and elec_hits > fire_hits
                and elec_hits > pipe_hits):
            return "elec"
        if (pipe_ratio >= self._THRESHOLD
                and pipe_hits > fire_hits
                and pipe_hits > elec_hits):
            return "pipe"

        return None  # 애매하거나 arch → ML 위임
