/*
 * File    : CadSllmAgent/Services/SocketClient.cs
 * Author  : 김지우
 * Date    : 2026-04-12
 * Modified: 2026-04-23 (자동 재연결 + 지수 백오프)
 * Description : Python 백엔드와 WebSocket 연결 유지.
 *               연결이 끊기면 최대 30초 간격으로 무한 재연결 시도.
 */
using System;
using System.Net.WebSockets;
using System.Collections.Concurrent;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autodesk.AutoCAD.ApplicationServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class SocketClient
    {
        private static ClientWebSocket _ws = new ClientWebSocket();
        private static readonly string _wsUri = AgentConfig.WebSocketUri;
        private static readonly ConcurrentQueue<string> _pendingSends = new();
        private const int MaxPendingSends = 100;

        // 재연결 제어
        private static volatile bool _stopped = false;
        private static int _retryDelay = 3_000;          // 현재 대기 시간(ms)
        private const int _retryDelayMin = 3_000;
        private const int _retryDelayMax = 30_000;
        private static CancellationTokenSource _retryCts = new CancellationTokenSource();

        /// <summary>WebSocket에 JSON 텍스트를 전송합니다.</summary>
        public static async Task SendAsync(string json)
        {
            if (_ws.State != WebSocketState.Open)
            {
                EnqueuePending(json);
                CadDebugLog.Warn($"WebSocket not open; queued outbound message. pending={_pendingSends.Count}");
                return;
            }
            var bytes = Encoding.UTF8.GetBytes(json);
            await _ws.SendAsync(new ArraySegment<byte>(bytes),
                WebSocketMessageType.Text, true, CancellationToken.None);
        }

        private static void EnqueuePending(string json)
        {
            while (_pendingSends.Count >= MaxPendingSends && _pendingSends.TryDequeue(out _)) { }
            _pendingSends.Enqueue(json);
        }

        private static async Task FlushPendingAsync()
        {
            while (_ws.State == WebSocketState.Open && _pendingSends.TryDequeue(out var msg))
            {
                var bytes = Encoding.UTF8.GetBytes(msg);
                await _ws.SendAsync(new ArraySegment<byte>(bytes),
                    WebSocketMessageType.Text, true, CancellationToken.None);
            }
        }

        public static bool IsConnected => _ws.State == WebSocketState.Open;

        /// <summary>플러그인 종료 시 호출 — 재연결 루프를 완전히 중단합니다.</summary>
        public static void StopAndDispose()
        {
            _stopped = true;
            _retryCts.Cancel();
            try { _ws.Abort(); } catch { /* 무시 */ }
        }

        /// <summary>
        /// WebSocket 연결을 시작합니다.
        /// 최초 연결 및 재연결 모두 이 메서드를 사용합니다.
        /// </summary>
        public static async Task ConnectAsync()
        {
            _stopped = false;
            _retryDelay = _retryDelayMin;
            _retryCts = new CancellationTokenSource();
            await ConnectInternalAsync();
        }

        private static async Task ConnectInternalAsync()
        {
            while (!_stopped)
            {
                var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;
                try
                {
                    // 기존 소켓 정리
                    try { _ws.Abort(); } catch { /* 무시 */ }
                    _ws = new ClientWebSocket();

                    await _ws.ConnectAsync(new Uri(_wsUri), CancellationToken.None);

                    _retryDelay = _retryDelayMin; // 성공 시 딜레이 초기화
                    CadDebugLog.Info($"WebSocket connected: {_wsUri}");
                    ed?.WriteMessage("\n[CAD-Agent] Python AI 서버에 연결되었습니다.\n");
                    await FlushPendingAsync();

                    // 연결 유지 — 끊길 때까지 블로킹
                    await ReceiveLoopAsync(ed);
                }
                catch (Exception ex)
                {
                    CadDebugLog.Exception("SocketClient.ConnectInternalAsync", ex);
                    ed?.WriteMessage($"\n[CAD-Agent] 서버 연결 실패: {ex.Message}\n");
                }

                if (_stopped) break;

                // 지수 백오프로 재연결 대기
                ed?.WriteMessage($"\n[CAD-Agent] {_retryDelay / 1000}초 후 재연결 시도...\n");
                try
                {
                    await Task.Delay(_retryDelay, _retryCts.Token);
                }
                catch (TaskCanceledException)
                {
                    break;
                }

                _retryDelay = Math.Min(_retryDelay * 2, _retryDelayMax);
            }
        }

        private static async Task ReceiveLoopAsync(Autodesk.AutoCAD.EditorInput.Editor? ed)
        {
            var frameBuffer = new byte[1024 * 64]; // 64KB 프레임 버퍼
            var msgStream   = new System.IO.MemoryStream();

            while (_ws.State == WebSocketState.Open)
            {
                try
                {
                    msgStream.SetLength(0);
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await _ws.ReceiveAsync(
                            new ArraySegment<byte>(frameBuffer), CancellationToken.None);
                        if (result.MessageType != WebSocketMessageType.Close)
                            msgStream.Write(frameBuffer, 0, result.Count);
                    }
                    while (!result.EndOfMessage);

                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        try
                        {
                            await _ws.CloseAsync(
                                WebSocketCloseStatus.NormalClosure, "Closing", CancellationToken.None);
                        }
                        catch { /* 무시 */ }
                        ed?.WriteMessage("\n[CAD-Agent] 서버와 연결이 종료되었습니다.\n");
                        return; // 상위 루프에서 재연결
                    }

                    string message = Encoding.UTF8.GetString(msgStream.ToArray());
                    SocketMessageHandler.HandleMessage(message);
                }
                catch (Exception ex) when (!_stopped)
                {
                    CadDebugLog.Exception("SocketClient.ReceiveLoopAsync", ex);
                    ed?.WriteMessage($"\n[CAD-Agent] WebSocket 수신 오류: {ex.Message}\n");
                    return; // 상위 루프에서 재연결
                }
            }
        }
    }
}
