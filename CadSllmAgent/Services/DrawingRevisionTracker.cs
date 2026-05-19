using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using CadSllmAgent.Extraction;
using CadSllmAgent.Models;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class DrawingRevisionTracker
    {
        private static readonly string[] IgnoredLayers =
        {
            "AI_REVIEW",
            "AI_REVIEW_LOW",
            "AI_ZOOM_HIGHLIGHT",
        };

        private static Document? _hookedDoc;
        private static volatile string _currentCadSessionId = "";
        private static volatile string _drawingId = "";
        private static int _suppressDepth = 0;
        private static int _localDirtyNotified = 0;
        private static readonly object _gate = new object();

        // 배치 버퍼 (lock(_gate)로 보호)
        private static readonly HashSet<string> _modifiedHandles  = new(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> _appendedHandles  = new(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> _erasedHandles    = new(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> _addedLayerNames  = new(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> _deletedLayerNames = new(StringComparer.OrdinalIgnoreCase);

        // 재시도 큐 (batch 유실 방지)
        private static readonly System.Collections.Concurrent.ConcurrentQueue<DirtyBatch>
            _retryQueue = new();

        // Background worker: 동시 실행 1개만 허용
        private static readonly SemaphoreSlim _workerSem = new SemaphoreSlim(1, 1);

        public static string CurrentCadSessionId => _currentCadSessionId;

        public static void RefreshSnapshotBaselineForActiveDocument(string reason = "document_activated")
        {
            SafeTaskDispatcher.EnqueueSafeTask(() =>
            {
                try
                {
                    var doc = AcApp.DocumentManager.MdiActiveDocument;
                    if (doc == null) return;

                    if (string.Equals(reason, "session_info", StringComparison.OrdinalIgnoreCase) &&
                        !string.IsNullOrWhiteSpace(_drawingId))
                    {
                        CadDebugLog.Info(
                            "[DrawingRevisionTracker] snapshot baseline skipped (session_info): context already active");
                        return;
                    }

                    if (string.IsNullOrWhiteSpace(PluginEntry.OrgId) ||
                        string.IsNullOrWhiteSpace(PluginEntry.DeviceId))
                    {
                        CadDebugLog.Info(
                            $"[DrawingRevisionTracker] snapshot baseline skipped ({reason}): missing org/device");
                        return;
                    }

                    var identityPath = ApiClient.GetActiveDrawingIdentityPath();
                    if (string.IsNullOrWhiteSpace(identityPath))
                    {
                        CadDebugLog.Info(
                            $"[DrawingRevisionTracker] snapshot baseline skipped ({reason}): missing drawing path");
                        return;
                    }

                    var drawingId = ApiClient.ComputeDrawingIdForPath(identityPath);
                    if (string.IsNullOrWhiteSpace(drawingId))
                    {
                        CadDebugLog.Info(
                            $"[DrawingRevisionTracker] snapshot baseline skipped ({reason}): missing drawing id");
                        return;
                    }

                    var sessionId = Guid.NewGuid().ToString();

                    SetDrawingContext(sessionId, drawingId);
                    HookDocumentEvents(forceRehook: true);
                    _ = Task.Run(() => RunFullSnapshotBaselineAsync(
                        drawingId, sessionId, reason, identityPath));
                }
                catch (Exception ex)
                {
                    CadDebugLog.Exception("DrawingRevisionTracker.RefreshSnapshotBaselineForActiveDocument", ex);
                }
            });
        }

        public static void SetDrawingContext(string sessionId, string drawingId)
        {
            _currentCadSessionId = sessionId ?? "";
            _drawingId           = drawingId ?? "";
            Interlocked.Exchange(ref _localDirtyNotified, 0);
            lock (_gate) { ClearAllSets(); }
            CadDebugLog.Info(
                $"[DrawingRevisionTracker] context set session={_currentCadSessionId} drawingId={_drawingId}");
        }

        [Obsolete("Use SetDrawingContext(sessionId, drawingId) instead. MarkSnapshotCached does not set _drawingId and dirty events will be silently dropped.")]
        public static void MarkSnapshotCached(string sessionId)
        {
            _currentCadSessionId = sessionId ?? "";
            CadDebugLog.Info($"[DrawingRevisionTracker] snapshot cached session={_currentCadSessionId}");
        }

        public static void HookDocumentEvents(bool forceRehook = false)
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            if (!forceRehook && ReferenceEquals(_hookedDoc, doc)) return;

            UnhookDocumentEvents();
            _hookedDoc = doc;
            doc.Database.ObjectModified += OnObjectModified;
            doc.Database.ObjectAppended += OnObjectAppended;
            doc.Database.ObjectErased   += OnObjectErased;
            doc.CommandEnded            += OnCommandEnded;
            CadDebugLog.Info($"[DrawingRevisionTracker] DB events hooked: {doc.Name}");
        }

        public static void UnhookDocumentEvents()
        {
            var doc = _hookedDoc;
            _hookedDoc = null;
            if (doc == null) return;

            try
            {
                doc.CommandEnded -= OnCommandEnded;
            }
            catch { /* AutoCAD shutdown/document dispose 중이면 무시 */ }

            try
            {
                var db = doc.Database;
                if (db == null) return;

                db.ObjectModified -= OnObjectModified;
                db.ObjectAppended -= OnObjectAppended;
                db.ObjectErased   -= OnObjectErased;
            }
            catch
            {
                // AutoCAD 종료 시점에는 Document/Database가 이미 dispose될 수 있다.
            }
        }

        public static void ResetForDocumentSwitch()
        {
            _currentCadSessionId = "";
            _drawingId = "";
            lock (_gate) { ClearAllSets(); }
        }

        public static IDisposable SuppressDirtyEvents()
        {
            Interlocked.Increment(ref _suppressDepth);
            return new SuppressionScope();
        }

        private sealed class SuppressionScope : IDisposable
        {
            private int _disposed;
            public void Dispose()
            {
                if (Interlocked.Exchange(ref _disposed, 1) == 0)
                    Interlocked.Decrement(ref _suppressDepth);
            }
        }

        private static void OnObjectModified(object sender, ObjectEventArgs e) => SafeMarkDirty(e.DBObject, "modified");
        private static void OnObjectAppended(object sender, ObjectEventArgs e) => SafeMarkDirty(e.DBObject, "appended");

        private static void OnObjectErased(object sender, ObjectErasedEventArgs e)
        {
            if (e.Erased) SafeMarkDirty(e.DBObject, "erased");
        }

        private static void SafeMarkDirty(DBObject? obj, string reason)
        {
            try { MarkDirty(obj, reason); }
            catch (Exception ex) { CadDebugLog.Exception("DrawingRevisionTracker.SafeMarkDirty", ex); }
        }

        private static void MarkDirty(DBObject? obj, string reason)
        {
            if (Volatile.Read(ref _suppressDepth) > 0) return;
            if (string.IsNullOrWhiteSpace(_drawingId)) return;

            bool marked = false;
            lock (_gate)
            {
                switch (obj)
                {
                    case LayerTableRecord ltr when reason == "appended":
                        _addedLayerNames.Add(ltr.Name);
                        marked = true;
                        break;
                    case LayerTableRecord ltr when reason == "erased":
                        _deletedLayerNames.Add(ltr.Name);
                        marked = true;
                        break;
                    case Entity ent:
                        if (IgnoredLayers.Any(x =>
                                string.Equals(x, ent.Layer, StringComparison.OrdinalIgnoreCase)))
                            return;
                        var handle = ent.Handle.ToString();
                        switch (reason)
                        {
                            case "modified": _modifiedHandles.Add(handle); break;
                            case "appended": _appendedHandles.Add(handle); break;
                            case "erased":   _erasedHandles.Add(handle);   break;
                        }
                        marked = true;
                        break;
                }
            }
            if (marked) NotifyLocalDirty();
        }

        private static DirtyBatch FlushBatch(string drawingId, string sessionId)
        {
            lock (_gate)
            {
                var erased   = new List<string>(_erasedHandles);
                var appended = _appendedHandles.Except(_erasedHandles,
                    StringComparer.OrdinalIgnoreCase).ToList();
                var modified = _modifiedHandles
                    .Except(_appendedHandles, StringComparer.OrdinalIgnoreCase)
                    .Except(_erasedHandles,   StringComparer.OrdinalIgnoreCase)
                    .ToList();

                var batch = new DirtyBatch
                {
                    Modified      = modified,
                    Appended      = appended,
                    Erased        = erased,
                    LayerAdded    = new List<string>(_addedLayerNames),
                    LayerDeleted  = new List<string>(_deletedLayerNames),
                    DrawingId     = drawingId,
                    SessionId     = sessionId,
                };
                ClearAllSets();
                return batch;
            }
        }

        private static void ClearAllSets()
        {
            _modifiedHandles.Clear();
            _appendedHandles.Clear();
            _erasedHandles.Clear();
            _addedLayerNames.Clear();
            _deletedLayerNames.Clear();
            Interlocked.Exchange(ref _localDirtyNotified, 0);
        }

        private static void NotifyLocalDirty()
        {
            if (Interlocked.Exchange(ref _localDirtyNotified, 1) == 1) return;
            var drawingId = _drawingId;
            var sessionId = _currentCadSessionId;
            if (string.IsNullOrWhiteSpace(drawingId)) return;

            _ = SocketClient.SendAsync(System.Text.Json.JsonSerializer.Serialize(new
            {
                action = "DRAWING_LOCAL_DIRTY",
                session_id = sessionId,
                payload = new
                {
                    drawing_id = drawingId,
                    session_id = sessionId,
                    dirty_count = 1,
                    message = "AutoCAD local drawing change detected",
                }
            }));
        }

        private static bool HasPendingChanges()
        {
            lock (_gate)
            {
                return _modifiedHandles.Count > 0 || _appendedHandles.Count > 0
                    || _erasedHandles.Count > 0   || _addedLayerNames.Count > 0
                    || _deletedLayerNames.Count > 0;
            }
        }

        // ── OnCommandEnded: UI 스레드는 FlushBatch 후 즉시 반환 ────────────────────
        private static string NormalizeIdentityPath(string identityPath)
        {
            return (identityPath ?? "").Replace('\\', '/').Trim();
        }

        private static bool IsSameIdentityPath(string expected, string actual)
        {
            return string.Equals(
                NormalizeIdentityPath(expected),
                NormalizeIdentityPath(actual),
                StringComparison.OrdinalIgnoreCase);
        }

        private static void DropQueuedBatchesForPreviousSessions(string drawingId, string currentSessionId)
        {
            if (string.IsNullOrWhiteSpace(drawingId)) return;

            var keep = new List<DirtyBatch>();
            while (_retryQueue.TryDequeue(out var batch))
            {
                var sameDrawing = string.Equals(
                    batch.DrawingId,
                    drawingId,
                    StringComparison.OrdinalIgnoreCase);
                var sameSession = string.Equals(
                    batch.SessionId,
                    currentSessionId,
                    StringComparison.OrdinalIgnoreCase);

                if (!sameDrawing || sameSession)
                    keep.Add(batch);
            }

            foreach (var batch in keep)
                _retryQueue.Enqueue(batch);
        }

        private static async void OnCommandEnded(object sender, CommandEventArgs e)
        {
            // OnCommandEnded 시점에 drawingId/sessionId 스냅샷 — background 처리 중 변경돼도 안전
            var drawingId = _drawingId;
            var sessionId = _currentCadSessionId;

            var hasPending = HasPendingChanges();
            var msg = $"OnCommandEnded cmd={e.GlobalCommandName} suppressDepth={_suppressDepth} " +
                      $"drawingId={(string.IsNullOrWhiteSpace(drawingId) ? "(없음)" : drawingId)} hasPending={hasPending}";
            _ = ApiClient.PostDebugLogAsync(msg, drawingId, sessionId);

            if (Volatile.Read(ref _suppressDepth) > 0) return;
            if (!HasPendingChanges()) return;
            if (string.IsNullOrWhiteSpace(drawingId)) return;

            // drawingId/sessionId를 배치에 심어 drain 중 도면 전환이 발생해도 올바른 ID 사용
            var batch = FlushBatch(drawingId, sessionId);
            if (!BatchHasChanges(batch))
            {
                _ = ApiClient.PostDebugLogAsync(
                    "OnCommandEnded flushed empty batch — skip dirty upload",
                    drawingId,
                    sessionId);
                return;
            }

            // UI 스레드 즉시 반환 — 네트워크 I/O 전체를 background worker에 위임
            _ = Task.Run(async () => await RunBatchOnBackgroundAsync(batch));
        }

        private static bool BatchHasChanges(DirtyBatch batch)
        {
            return (batch.Modified?.Count ?? 0) > 0
                || (batch.Appended?.Count ?? 0) > 0
                || (batch.Erased?.Count ?? 0) > 0
                || (batch.LayerAdded?.Count ?? 0) > 0
                || (batch.LayerDeleted?.Count ?? 0) > 0;
        }

        // ── Background worker (SemaphoreSlim으로 동시 1개 보장) ──────────────────
        // 각 batch는 생성 시점의 DrawingId/SessionId를 포함하므로 도면 전환 후에도 안전
        private static async Task RunBatchOnBackgroundAsync(DirtyBatch batch)
        {
            // Worker 이미 실행 중이면 retry queue에 보존 후 반환.
            // 현재 worker가 끝날 때 queue를 재확인하여 kick하므로 유실 없음.
            if (!await _workerSem.WaitAsync(0))
            {
                _retryQueue.Enqueue(batch);
                return;
            }

            bool hadFailure = false;
            try
            {
                // 현재 batch를 queue 끝에 추가하여 drain loop로 통합 처리
                _retryQueue.Enqueue(batch);

                while (_retryQueue.TryDequeue(out var current))
                {
                    if (!BatchHasChanges(current))
                        continue;

                    try
                    {
                        // batch 자체의 DrawingId/SessionId 사용 — drain 중 도면 전환 무관
                        var response = await ApiClient.SendDirtyBatchAsync(
                            current.DrawingId, current.SessionId, current);
                        await HandleNextModeAsync(
                            current.DrawingId, current.SessionId, current, response);
                    }
                    catch (Exception ex)
                    {
                        CadDebugLog.Exception("DrawingRevisionTracker.RunBatchOnBackgroundAsync (drain)", ex);
                        _retryQueue.Enqueue(current); // 유실 방지
                        hadFailure = true;
                        break;
                    }
                }
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("DrawingRevisionTracker.RunBatchOnBackgroundAsync", ex);
                _retryQueue.Enqueue(batch);
                hadFailure = true;
            }
            finally
            {
                _workerSem.Release();
                // 처리 중 새로 쌓인 항목이 있고 실패가 없었으면 drain kick
                // 실패한 경우엔 다음 CommandEnded 때 자동 재시도됨
                if (!hadFailure && !_retryQueue.IsEmpty)
                    _ = Task.Run(DrainRetryQueueAsync);
            }
        }

        // ── Retry drain: CommandEnded 없이도 queue 소진 보장 ────────────────────
        private static async Task DrainRetryQueueAsync()
        {
            if (!await _workerSem.WaitAsync(0)) return; // 다른 worker가 이미 처리 중

            bool hadFailure = false;
            try
            {
                while (_retryQueue.TryDequeue(out var batch))
                {
                    if (!BatchHasChanges(batch))
                        continue;

                    try
                    {
                        var response = await ApiClient.SendDirtyBatchAsync(
                            batch.DrawingId, batch.SessionId, batch);
                        await HandleNextModeAsync(
                            batch.DrawingId, batch.SessionId, batch, response);
                    }
                    catch (Exception ex)
                    {
                        CadDebugLog.Exception("DrawingRevisionTracker.DrainRetryQueueAsync", ex);
                        _retryQueue.Enqueue(batch);
                        hadFailure = true;
                        break;
                    }
                }
            }
            finally
            {
                _workerSem.Release();
                if (!hadFailure && !_retryQueue.IsEmpty)
                    _ = Task.Run(DrainRetryQueueAsync);
            }
        }

        // ── AutoCAD main context marshal helper ──────────────────────────────────
        // AutoCAD DB 접근은 반드시 이 헬퍼를 통해 ExecuteInApplicationContext로 marshal
        private static Task<T?> MarshalToAcContextAsync<T>(Func<T?> action) where T : class
        {
            return SafeTaskDispatcher.EnqueueSafeTaskAsync(action);
        }

        // ── HandleNextModeAsync (drawingId/sessionId 명시 인자) ──────────────────
        private static async Task HandleNextModeAsync(
            string drawingId, string sessionId, DirtyBatch batch, DirtyBatchResponse response)
        {
            switch (response.NextMode)
            {
                case "INCREMENTAL":
                    await HandleIncrementalAsync(drawingId, sessionId, batch, response);
                    break;
                case "FULL_RESYNC":
                    await HandleFullResyncAsync(drawingId, sessionId, response);
                    break;
                case "WAIT":
                    CadDebugLog.Info(
                        $"[DrawingRevisionTracker] WAIT reason={response.Reason}");
                    break;
            }
        }

        // ── INCREMENTAL: ExtractByHandles는 main context marshal, 나머지는 background ──
        private static async Task HandleIncrementalAsync(
            string drawingId, string sessionId, DirtyBatch batch, DirtyBatchResponse response)
        {
            if (string.IsNullOrEmpty(response.CurrentSnapshotId))
            {
                CadDebugLog.Error("[DrawingRevisionTracker] Incremental skipped: missing snapshot id — requeueing batch");
                _ = ApiClient.PostDebugLogAsync(
                    "Incremental skipped: missing snapshot id — requeueing batch",
                    drawingId,
                    sessionId);
                _retryQueue.Enqueue(batch);
                return;
            }

            var allHandles = batch.Modified.Concat(batch.Appended).ToHashSet(
                StringComparer.OrdinalIgnoreCase);
            _ = ApiClient.PostDebugLogAsync(
                $"Incremental start modified={batch.Modified.Count} appended={batch.Appended.Count} erased={batch.Erased.Count} handles={allHandles.Count}",
                drawingId,
                sessionId);

            List<CadEntity> changedEntities = new List<CadEntity>();
            if (allHandles.Count > 0)
            {
                // AutoCAD DB 접근: main context로 marshal
                var extractTask = MarshalToAcContextAsync(() =>
                {
                    var doc = AcApp.DocumentManager.MdiActiveDocument;
                    if (doc == null) return null;
                    using (doc.LockDocument())
                        return CadDataExtractor.ExtractByHandles(doc, allHandles);
                });
                var completed = await Task.WhenAny(extractTask, Task.Delay(TimeSpan.FromSeconds(10)));
                if (!ReferenceEquals(completed, extractTask))
                {
                    CadDebugLog.Error("[DrawingRevisionTracker] Incremental extract timeout — requeueing batch");
                    _ = ApiClient.PostDebugLogAsync(
                        "Incremental extract timeout — requeueing batch",
                        drawingId,
                        sessionId);
                    _retryQueue.Enqueue(batch);
                    return;
                }

                var extracted = await extractTask;
                if (extracted == null)
                {
                    CadDebugLog.Error("[DrawingRevisionTracker] Incremental extract returned null — requeueing batch");
                    _ = ApiClient.PostDebugLogAsync(
                        "Incremental extract returned null — requeueing batch",
                        drawingId,
                        sessionId);
                    _retryQueue.Enqueue(batch);
                    return;
                }
                changedEntities = extracted;
            }
            _ = ApiClient.PostDebugLogAsync(
                $"Incremental extracted entities={changedEntities.Count}",
                drawingId,
                sessionId);

            // presign / S3 PUT / commit — background thread에서 실행
            _ = ApiClient.PostDebugLogAsync("Incremental requesting delta presign", drawingId, sessionId);
            var presign = await ApiClient.RequestDeltaPresignAsync(drawingId);
            if (presign == null)
            {
                CadDebugLog.Error("[DrawingRevisionTracker] RequestDeltaPresignAsync failed — requeueing batch");
                _ = ApiClient.PostDebugLogAsync(
                    "RequestDeltaPresignAsync failed — requeueing batch",
                    drawingId,
                    sessionId);
                _retryQueue.Enqueue(batch);
                return;
            }

            var delta = new
            {
                drawing_id        = drawingId,
                delta_id          = presign.DeltaId,
                base_snapshot_id  = response.CurrentSnapshotId,
                created_at        = DateTime.UtcNow.ToString("o"),
                modified          = changedEntities.Where(e => batch.Modified.Contains(
                    e.Handle, StringComparer.OrdinalIgnoreCase)),
                appended          = changedEntities.Where(e => batch.Appended.Contains(
                    e.Handle, StringComparer.OrdinalIgnoreCase)),
                erased            = batch.Erased,
                layer_added       = batch.LayerAdded.Select(n => new { name = n }),
                layer_deleted     = batch.LayerDeleted,
                changed_counts    = new {
                    modified      = batch.Modified.Count,
                    appended      = batch.Appended.Count,
                    erased        = batch.Erased.Count,
                    layer_added   = batch.LayerAdded.Count,
                    layer_deleted = batch.LayerDeleted.Count,
                },
            };

            var deltaJson = System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(delta);
            _ = ApiClient.PostDebugLogAsync($"Incremental delta json size={deltaJson.Length}", drawingId, sessionId);
            await ApiClient.PutToS3Async(presign.UploadUrl, deltaJson);

            bool ok = await ApiClient.CommitDeltaAsync(drawingId, new DeltaCommitRequest
            {
                DeltaId        = presign.DeltaId,
                S3Key          = presign.S3Key,
                S3Uri          = presign.S3Uri,
                DeltaSize      = deltaJson.Length,
                BaseSnapshotId = response.CurrentSnapshotId,
                ChangedHandles = new()
                {
                    ["modified"]      = batch.Modified,
                    ["appended"]      = batch.Appended,
                    ["erased"]        = batch.Erased,
                    ["layer_added"]   = batch.LayerAdded,
                    ["layer_deleted"] = batch.LayerDeleted,
                },
                SessionId = sessionId,
                OrgId     = string.IsNullOrEmpty(PluginEntry.OrgId) ? "unknown" : PluginEntry.OrgId,
            });
            if (!ok)
            {
                CadDebugLog.Error("[DrawingRevisionTracker] CommitDeltaAsync failed — requeueing batch");
                _ = ApiClient.PostDebugLogAsync(
                    "CommitDeltaAsync failed — requeueing batch",
                    drawingId,
                    sessionId);
                _retryQueue.Enqueue(batch);
            }
            else
            {
                _ = ApiClient.PostDebugLogAsync("CommitDeltaAsync ok", drawingId, sessionId);
            }
        }

        // ── FULL_RESYNC: ExtractFull은 main context marshal, 나머지는 background ──
        private static async Task HandleFullResyncAsync(
            string drawingId, string sessionId, DirtyBatchResponse response)
        {
            await CommitFullSnapshotWithResyncAsync(drawingId, sessionId, "full_resync");
        }

        private static async Task RunFullSnapshotBaselineAsync(
            string drawingId, string sessionId, string reason, string identityPath)
        {
            await _workerSem.WaitAsync();
            try
            {
                DropQueuedBatchesForPreviousSessions(drawingId, sessionId);

                var activeCheck = await MarshalToAcContextAsync(() =>
                {
                    var activeIdentityPath = ApiClient.GetActiveDrawingIdentityPath();
                    return IsSameIdentityPath(identityPath, activeIdentityPath) ? "active" : null;
                });
                if (activeCheck == null)
                {
                    CadDebugLog.Info(
                        $"[DrawingRevisionTracker] full snapshot baseline skipped before resync: active document changed reason={reason}");
                    return;
                }

                await CommitFullSnapshotWithResyncAsync(
                    drawingId, sessionId, reason, identityPath);
                lock (_gate) { ClearAllSets(); }
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("DrawingRevisionTracker.RunFullSnapshotBaselineAsync", ex);
            }
            finally
            {
                _workerSem.Release();
                if (!_retryQueue.IsEmpty)
                    _ = Task.Run(DrainRetryQueueAsync);
            }
        }

        private static async Task CommitFullSnapshotWithResyncAsync(
            string drawingId, string sessionId, string reason, string? expectedIdentityPath = null)
        {
            using var startContent = new StringContent("{}", Encoding.UTF8, "application/json");
            using var startResp = await ApiClient._httpClient.PostAsync(
                $"/api/v1/cad/drawings/{drawingId}/resync/start", startContent);
            if (!startResp.IsSuccessStatusCode) return;
            var startJson = await startResp.Content.ReadAsStringAsync();
            var startResult = System.Text.Json.JsonSerializer.Deserialize<ResyncStartResponse>(
                startJson, ApiClient._jsonOptions);
            if (startResult == null || !startResult.Acquired) return;

            try
            {
                // AutoCAD DB 접근: main context로 marshal (60s 타임아웃 — modal dialog 등 AutoCAD가 응답 불가 상태 대비)
                CadDebugLog.Info(
                    $"[DrawingRevisionTracker] full snapshot baseline start reason={reason} drawingId={drawingId}");
                var marshalTask = MarshalToAcContextAsync(() =>
                {
                    var doc = AcApp.DocumentManager.MdiActiveDocument;
                    if (doc == null) return null;
                    if (!string.IsNullOrWhiteSpace(expectedIdentityPath))
                    {
                        var activeIdentityPath = ApiClient.GetActiveDrawingIdentityPath();
                        if (!IsSameIdentityPath(expectedIdentityPath, activeIdentityPath))
                        {
                            CadDebugLog.Info(
                                $"[DrawingRevisionTracker] full snapshot baseline skipped: active document changed reason={reason}");
                            return null;
                        }
                    }
                    using (doc.LockDocument())
                        return CadDataExtractor.ExtractFull(doc);
                });
                var completedTask = await Task.WhenAny(marshalTask, Task.Delay(TimeSpan.FromSeconds(60)));
                if (!ReferenceEquals(completedTask, marshalTask))
                {
                    CadDebugLog.Error("[DrawingRevisionTracker] FullResync extract timeout (60s) — calling resync fail");
                    _ = ApiClient.PostDebugLogAsync(
                        "FullResync extract timeout (60s) — calling resync fail",
                        drawingId, sessionId);
                    await CallResyncFailAsync(drawingId, startResult.ResyncToken, "extract timeout (60s)");
                    return;
                }

                var fullDrawing = await marshalTask;
                if (fullDrawing == null)
                {
                    await CallResyncFailAsync(drawingId, startResult.ResyncToken, "doc is null");
                    return;
                }

                // presign / S3 PUT / commit — background thread에서 실행
                var presign = await ApiClient.RequestSnapshotPresignAsync(drawingId);
                if (presign == null) throw new Exception("presign 실패");

                var snapshotBytes = System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(fullDrawing);
                await ApiClient.PutToS3Async(presign.UploadUrl, snapshotBytes);

                var revHash = Convert.ToHexString(
                    System.Security.Cryptography.SHA256.HashData(snapshotBytes))[..16];
                bool committed = await ApiClient.CommitSnapshotAsync(drawingId, new SnapshotCommitRequest
                {
                    SnapshotId   = presign.SnapshotId,
                    S3Key        = presign.S3Key,
                    S3Uri        = presign.S3Uri,
                    EntityCount  = fullDrawing.Entities?.Count ?? 0,
                    RevisionHash = revHash,
                    ResyncToken  = startResult.ResyncToken,
                    SessionId    = sessionId,
                    OrgId        = string.IsNullOrEmpty(PluginEntry.OrgId) ? "unknown" : PluginEntry.OrgId,
                });
                if (!committed)
                    throw new Exception("CommitSnapshotAsync returned false");
                CadDebugLog.Info(
                    $"[DrawingRevisionTracker] full snapshot baseline committed reason={reason} drawingId={drawingId}");
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("DrawingRevisionTracker.CommitFullSnapshotWithResyncAsync", ex);
                await CallResyncFailAsync(drawingId, startResult!.ResyncToken, ex.Message);
            }
        }

        private static async Task CallResyncFailAsync(string drawingId, string? token, string error)
        {
            try
            {
                var body = System.Text.Json.JsonSerializer.Serialize(
                    new { resync_token = token, error }, ApiClient._jsonOptions);
                using var content = new StringContent(body, Encoding.UTF8, "application/json");
                await ApiClient._httpClient.PostAsync(
                    $"/api/v1/cad/drawings/{drawingId}/resync/fail", content);
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("DrawingRevisionTracker.CallResyncFailAsync", ex);
            }
        }

        // SocketMessageHandler 에서 auto-fix 완료 후 호출 — semaphore 보호된 background worker로 라우팅
        public static Task CommitAutoFixDeltaAsync(List<string> fixedHandles, bool appended = false)
        {
            var batch = appended
                ? new DirtyBatch { Appended = fixedHandles, DrawingId = _drawingId, SessionId = _currentCadSessionId }
                : new DirtyBatch { Modified = fixedHandles, DrawingId = _drawingId, SessionId = _currentCadSessionId };
            return Task.Run(() => RunBatchOnBackgroundAsync(batch));
        }
    }
}
