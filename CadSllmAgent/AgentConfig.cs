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
        /// <summary>FastAPI 백엔드 REST 기본 주소</summary>
        public const string BackendBaseUrl = "http://127.0.0.1:8000";

        /// <summary>WebSocket 연결 URI (cad 그룹)</summary>
        public const string WebSocketUri = "ws://127.0.0.1:8000/ws/cad";

        /// <summary>React 프론트엔드 URL</summary>
        public const string FrontendBaseUrl = "http://127.0.0.1:5173/";

        /// <summary>도면 HTTP 전송 시 청크당 엔티티 최대 수</summary>
        public const int ExtractionEntityChunkSize = 800;
    }
}
