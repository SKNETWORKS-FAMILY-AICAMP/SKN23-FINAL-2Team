"""
File    : backend/services/agents/common/domain_classifier/classifier.py
Author  : 김다빈
Create  : 2026-04-21
Modified: 2026-04-26

Description :
    도메인 분류기 하이브리드 래퍼.
    1단계: RuleClassifier — 명확한 CAD 블록 시그니처 도면을 100% 정확도로 처리
    2단계: XGBoost + Platt Scaling ML 분류기 — 규칙으로 못 잡은 케이스 처리

    기존 predict() / predict_proba() 인터페이스 유지 — agent_service.py 무수정.

    사용처: backend/services/agent_service.py
        → domain="auto" 처리 시 DomainClassifier.predict() 호출

    모델 경로: backend/api/classifier_models/domain_classifier.pkl  (XGBoost)
               backend/api/classifier_models/label_encoder.pkl
               backend/api/classifier_models/domain_classifier_meta.json

Modification History :
    2026-04-21 | 김다빈 | 최초 작성 (RF 래퍼)
    2026-04-26 | 김다빈 | RuleClassifier 선처리 레이어 추가 (하이브리드 구조)
                          XGBoost + Platt Scaling 모델로 교체 (노트북 재학습 필요)
"""

import json
import logging
from pathlib import Path

import numpy as np

from backend.services.agents.common.domain_classifier.feature_extractor import extract_features
from backend.services.agents.common.domain_classifier.rule_classifier import RuleClassifier

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_MODEL_DIR    = _PROJECT_ROOT / "backend" / "api" / "classifier_models"

DOMAIN_LABELS = ["arch", "elec", "fire", "pipe"]


class DomainClassifier:
    """
    CAD JSON → 도메인 분류기 (하이브리드: 규칙 선처리 + XGBoost ML).

    Parameters
    ----------
    model_dir : str | Path | None
        모델 파일 디렉토리. None이면 기본 경로(_MODEL_DIR) 사용.

    Examples
    --------
    >>> clf = DomainClassifier()
    >>> domain = clf.predict(cad_json)  # "arch" | "elec" | "fire" | "pipe"
    """

    def __init__(self, model_dir: str | Path | None = None) -> None:
        self._model_dir = Path(model_dir) if model_dir else _MODEL_DIR
        self._rule    = RuleClassifier()
        self._model   = None
        self._encoder = None
        self._meta: dict = {}
        self._loaded = False
        self._load()

    # ──────────────────────────────────────────────────────────────────────
    # Private
    # ──────────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            import joblib
        except ImportError:
            logger.error("joblib 없음 — pip install joblib 필요")
            return

        model_path   = self._model_dir / "domain_classifier.pkl"
        encoder_path = self._model_dir / "label_encoder.pkl"
        meta_path    = self._model_dir / "domain_classifier_meta.json"

        if not model_path.exists():
            logger.error(f"모델 파일 없음: {model_path}")
            return
        if not encoder_path.exists():
            logger.error(f"레이블 인코더 없음: {encoder_path}")
            return

        self._model   = joblib.load(model_path)
        self._encoder = joblib.load(encoder_path)

        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                self._meta = json.load(f)

        self._loaded = True
        # rule_classifier_coverage_pct 는 0~100 스케일의 percentage 값 (예: 0.51 = 0.51%)
        raw_pct = self._meta.get("rule_classifier_coverage_pct", 0.0)
        logger.info(
            "도메인 분류기 로드 완료 — "
            f"모델={self._meta.get('model_name', 'unknown')}, "
            f"클래스={self._meta.get('classes', [])}, "
            f"RuleClassifier 커버리지={raw_pct:.2f}%"
        )

    def _ml_predict(self, cad_json: dict) -> str:
        """ML 모델로 단일 도메인 예측 (규칙 선처리 건너뜀)."""
        feat = extract_features(cad_json).reshape(1, -1)
        try:
            pred_encoded = self._model.predict(feat)
            return str(self._encoder.inverse_transform(pred_encoded)[0])
        except Exception as e:
            logger.error(f"ML 분류 오류: {e} — 폴백 'arch' 반환")
            return "arch"

    def _ml_predict_proba(self, cad_json: dict) -> dict[str, float]:
        """ML 모델로 도메인별 확률 예측 (규칙 선처리 건너뜀)."""
        feat = extract_features(cad_json).reshape(1, -1)
        try:
            proba = self._model.predict_proba(feat)[0]
            return {str(cls): float(p) for cls, p in zip(self._encoder.classes_, proba)}
        except Exception as e:
            logger.error(f"ML 확률 추론 오류: {e}")
            return {d: 0.25 for d in DOMAIN_LABELS}

    # ──────────────────────────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def meta(self) -> dict:
        return self._meta

    def predict(self, cad_json: dict) -> str:
        """
        CAD JSON → 도메인 문자열.

        1단계: 규칙 선처리 (소방/전기/배관 블록 시그니처 매칭)
        2단계: ML 분류 (규칙이 None 반환한 경우)

        Returns
        -------
        str  "arch" | "elec" | "fire" | "pipe"
             모델 미로드 시 "arch" 폴백
        """
        if not isinstance(cad_json, dict):
            raise ValueError(f"cad_json은 dict여야 함, 받은 타입: {type(cad_json)}")

        rule_result = self._rule.predict(cad_json)
        if rule_result is not None:
            logger.debug(f"규칙 선처리 → {rule_result}")
            return rule_result

        if not self._loaded:
            logger.warning("분류기 미로드 — 폴백 도메인 'arch' 반환")
            return "arch"

        return self._ml_predict(cad_json)

    def predict_proba(self, cad_json: dict) -> dict[str, float]:
        """
        도메인별 확률값 반환.

        규칙 선처리가 확정한 도메인은 확률 1.0으로 반환 (보정 불필요).
        규칙 미확정 케이스는 XGBoost + Platt Scaling 보정 확률 반환.

        Returns
        -------
        dict[str, float]  {"arch": 0.9, "elec": 0.05, "fire": 0.03, "pipe": 0.02}
        """
        rule_result = self._rule.predict(cad_json)
        if rule_result is not None:
            return {rule_result: 1.0}

        if not self._loaded:
            logger.warning("분류기 미로드 — 균등 확률 반환")
            return {d: 0.25 for d in DOMAIN_LABELS}

        return self._ml_predict_proba(cad_json)

    def predict_batch(self, cad_json_list: list[dict]) -> list[str]:
        """
        여러 CAD JSON을 한번에 분류. 순서 유지.

        각 항목에 규칙 선처리 적용 후 ML 위임 항목만 일괄 처리.
        """
        if not cad_json_list:
            return []

        results: list[str | None] = [self._rule.predict(j) for j in cad_json_list]

        ml_indices = [i for i, r in enumerate(results) if r is None]

        if not ml_indices:
            return [r for r in results]  # type: ignore[return-value]

        if not self._loaded:
            logger.warning("분류기 미로드 — 폴백 'arch' 반환")
            for i in ml_indices:
                results[i] = "arch"
            return results  # type: ignore[return-value]

        ml_inputs = [cad_json_list[i] for i in ml_indices]
        feats = np.stack([extract_features(j) for j in ml_inputs])

        try:
            pred_encoded = self._model.predict(feats)
            ml_labels = [str(d) for d in self._encoder.inverse_transform(pred_encoded)]
        except Exception as e:
            logger.error(f"배치 ML 분류 오류: {e}")
            ml_labels = ["arch"] * len(ml_indices)

        for idx, label in zip(ml_indices, ml_labels):
            results[idx] = label

        return results  # type: ignore[return-value]
