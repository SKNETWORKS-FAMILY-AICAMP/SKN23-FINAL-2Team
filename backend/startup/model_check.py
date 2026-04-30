"""
File    : backend/startup/model_check.py
Description : 서버 시작 시 임베딩/리랭커 모델 존재 확인 및 자동 다운로드
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 모델 식별자 ──────────────────────────────────────────────────────────────
_BGE_M3_REPO   = "BAAI/bge-m3"
_QWEN_REPO     = "Qwen/Qwen3-Reranker-0.6B"
_QWEN_DIR_NAME = "Qwen__Qwen3-Reranker-0.6B"      # 로컬 저장 디렉터리 이름

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent   # SKN23-FINAL-2TEAM/


# ── HF 캐시 확인 헬퍼 ────────────────────────────────────────────────────────
def _hf_cache_root() -> Path:
    """
    HuggingFace 캐시 루트를 반환합니다.
    우선순위: HF_HUB_CACHE env → HF_HOME env/hub → huggingface_hub 내부 상수 → OS 기본값
    하드코딩 없이 현재 실행 환경의 경로를 동적으로 결정합니다.
    """
    # 1) 명시적 환경변수
    hf_hub_cache = os.environ.get("HF_HUB_CACHE")
    if hf_hub_cache:
        return Path(hf_hub_cache)

    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"

    # 2) huggingface_hub 라이브러리 내부 상수 (버전별 위치가 다름)
    for attr_path in (
        "huggingface_hub.constants.HF_HUB_CACHE",
        "huggingface_hub.file_download.HUGGINGFACE_HUB_CACHE",
    ):
        module, _, attr = attr_path.rpartition(".")
        try:
            import importlib
            mod = importlib.import_module(module)
            return Path(getattr(mod, attr))
        except Exception:
            continue

    # 3) XDG / OS 표준 기본값 (Path.home()은 현재 사용자 홈을 동적으로 반환)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _hf_model_cached(repo_id: str) -> bool:
    """HF 허브 캐시에 해당 모델 스냅샷이 존재하는지 확인."""
    dir_name = "models--" + repo_id.replace("/", "--")
    snap_dir = _hf_cache_root() / dir_name / "snapshots"
    if not snap_dir.exists():
        return False
    # snapshots/ 하위에 실제 체크포인트 폴더가 하나 이상 있어야 함
    return any(True for _ in snap_dir.iterdir())


# ── 다운로드 헬퍼 ─────────────────────────────────────────────────────────────
def _allow_hf_network():
    """HF 오프라인 환경변수를 잠시 해제하는 컨텍스트 매니저."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    return _ctx()


def _download_bge_m3():
    """BGE-M3 모델을 HF 캐시로 다운로드."""
    logger.info("[model_check] BGE-M3 다운로드 시작: %s", _BGE_M3_REPO)
    try:
        from huggingface_hub import snapshot_download
        with _allow_hf_network():
            snapshot_download(repo_id=_BGE_M3_REPO)
        logger.info("[model_check] BGE-M3 다운로드 완료")
    except Exception as e:
        logger.error("[model_check] BGE-M3 다운로드 실패: %s", e)


def _download_qwen_reranker(target_dir: Path):
    """Qwen3-Reranker 모델을 target_dir 에 다운로드."""
    logger.info("[model_check] Qwen3-Reranker 다운로드 시작 → %s", target_dir)
    try:
        from huggingface_hub import snapshot_download
        target_dir.mkdir(parents=True, exist_ok=True)
        with _allow_hf_network():
            snapshot_download(repo_id=_QWEN_REPO, local_dir=str(target_dir))
        logger.info("[model_check] Qwen3-Reranker 다운로드 완료")
    except Exception as e:
        logger.error("[model_check] Qwen3-Reranker 다운로드 실패: %s", e)


# ── 리랭커 경로 확인 ─────────────────────────────────────────────────────────
def _qwen_is_available() -> tuple[bool, Path | None]:
    """
    (available, local_path_or_None)
    로컬 디렉터리 우선, 없으면 HF 캐시 확인.
    """
    try:
        from backend.core.config import settings
        env_p = (settings.QWEN3_RERANKER_LOCAL_PATH or "").strip()
    except Exception:
        env_p = ""

    if env_p:
        p = Path(env_p).expanduser()
        if p.is_dir() and any(p.iterdir()):
            return True, p

    default_dir = _REPO_ROOT / "models" / _QWEN_DIR_NAME
    if default_dir.is_dir() and any(default_dir.iterdir()):
        return True, default_dir

    if _hf_model_cached(_QWEN_REPO):
        return True, None   # HF 캐시에 있음

    return False, default_dir   # 없음 → default_dir 에 다운로드


# ── 공개 진입점 ───────────────────────────────────────────────────────────────
def ensure_models_ready():
    """
    BGE-M3 임베딩 모델과 Qwen3-Reranker 모델이 로컬에 있는지 확인하고,
    없으면 HuggingFace Hub에서 자동 다운로드합니다.
    uvicorn lifespan에서 호출하세요.
    """
    logger.info("[model_check] 모델 준비 상태 확인 중...")

    # ── BGE-M3 ──────────────────────────────────────────────────────────
    if _hf_model_cached(_BGE_M3_REPO):
        logger.info("[model_check] BGE-M3 캐시 확인됨 ✓")
    else:
        logger.warning("[model_check] BGE-M3 캐시 없음 → 다운로드 시작")
        _download_bge_m3()

    # ── Qwen3-Reranker ───────────────────────────────────────────────────
    available, path = _qwen_is_available()
    if available:
        logger.info("[model_check] Qwen3-Reranker 확인됨 ✓ (path=%s)", path)
    else:
        logger.warning("[model_check] Qwen3-Reranker 없음 → 다운로드 시작 (→ %s)", path)
        _download_qwen_reranker(path)  # type: ignore[arg-type]

    logger.info("[model_check] 모델 준비 완료")
