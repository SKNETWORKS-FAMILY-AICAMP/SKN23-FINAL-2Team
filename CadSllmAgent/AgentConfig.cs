/*
 * File    : CadSllmAgent/AgentConfig.cs
 * Author  : 김지우
 * Create  : 2026-04-24
 * Description : 백엔드 URL, WebSocket URI, 프론트엔드 URL 등 환경 상수 중앙 관리.
 *               배포 환경 전환 시 이 파일만 수정한다.
 */

namespace CadSllmAgent
{
    public static class AgentConfig
    {
#if DEBUG
        // ─── 로컬 개발 환경 ────────────────────────────────────────────────
        /// <summary>FastAPI 백엔드 REST 기본 주소</summary>
        public const string BackendBaseUrl = "http://127.0.0.1:8000";

        /// <summary>WebSocket 연결 URI (cad 그룹)</summary>
        public const string WebSocketUri = "ws://127.0.0.1:8000/ws/cad";

        /// <summary>React 프론트엔드 URL</summary>
        public const string FrontendBaseUrl = "http://localhost:5173/";
#else
        // ─── EC2 운영 환경 ────────────────────────────────────────────────
        /// <summary>FastAPI 백엔드 REST 기본 주소</summary>
        public const string BackendBaseUrl = "http://15.164.110.240:8000";

        /// <summary>WebSocket 연결 URI (cad 그룹)</summary>
        public const string WebSocketUri = "ws://15.164.110.240:8000/ws/cad";

        /// <summary>React 프론트엔드 URL</summary>
        public const string FrontendBaseUrl = "http://15.164.110.240:5173/";
#endif

        public const string LocalFrontendBaseUrl = "http://localhost:5173/";
        public const bool PreferLocalFrontendWhenAvailable = true;

        /// <summary>도면 HTTP 전송 시 청크당 엔티티 최대 수</summary>
        public const int ExtractionEntityChunkSize = 800;

        // ── 플러그인 자동 업데이트 설정 ──────────────────────────────────
        /// <summary>플러그인 자동 업데이트 활성화 여부</summary>
        public const bool AutoUpdateEnabled = true;

        /// <summary>현재 설치 버전 정보가 기록된 파일명</summary>
        public const string VersionFileName = "version.txt";

        /// <summary>업데이트 실행 파일명 (CAD 종료 후 파일 교체 수행)</summary>
        public const string UpdaterExeName = "updater.exe";
    }
}
