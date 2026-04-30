using System;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
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
        private static string _currentCadSessionId = "";
        private static int _revision = 0;
        private static int _suppressDepth = 0;
        private static CancellationTokenSource? _dirtyDebounceCts;

        public static string CurrentCadSessionId => _currentCadSessionId;

        public static void MarkSnapshotCached(string sessionId)
        {
            _currentCadSessionId = sessionId ?? "";
            _revision = 0;
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
            doc.Database.ObjectErased += OnObjectErased;
            CadDebugLog.Info($"[DrawingRevisionTracker] DB events hooked: {doc.Name}");
        }

        public static void UnhookDocumentEvents()
        {
            if (_hookedDoc == null) return;
            try
            {
                _hookedDoc.Database.ObjectModified -= OnObjectModified;
                _hookedDoc.Database.ObjectAppended -= OnObjectAppended;
                _hookedDoc.Database.ObjectErased -= OnObjectErased;
            }
            catch { }
            _hookedDoc = null;
        }

        public static void ResetForDocumentSwitch()
        {
            _currentCadSessionId = "";
            _revision = 0;
            _dirtyDebounceCts?.Cancel();
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
            try
            {
                MarkDirty(obj, reason);
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("DrawingRevisionTracker.SafeMarkDirty", ex);
            }
        }

        private static void MarkDirty(DBObject? obj, string reason)
        {
            if (Volatile.Read(ref _suppressDepth) > 0) return;
            if (obj is not Entity ent) return;
            if (IgnoredLayers.Any(x => string.Equals(x, ent.Layer, StringComparison.OrdinalIgnoreCase))) return;
            if (string.IsNullOrWhiteSpace(_currentCadSessionId)) return;

            var sessionId = _currentCadSessionId;
            var layer = ent.Layer;
            var handle = ent.Handle.ToString();
            _revision++;
            var revision = _revision;
            _dirtyDebounceCts?.Cancel();
            var cts = new CancellationTokenSource();
            _dirtyDebounceCts = cts;

            _ = System.Threading.Tasks.Task.Run(async () =>
            {
                try
                {
                    await System.Threading.Tasks.Task.Delay(1200, cts.Token);
                    if (cts.IsCancellationRequested) return;

                    var msg = JsonSerializer.Serialize(new
                    {
                        action = "CAD_DRAWING_DIRTY",
                        session_id = sessionId,
                        payload = new
                        {
                            session_id = sessionId,
                            revision,
                            reason,
                            layer,
                            handle,
                        }
                    });
                    await SocketClient.SendAsync(msg);
                    if (string.Equals(_currentCadSessionId, sessionId, StringComparison.OrdinalIgnoreCase))
                        _currentCadSessionId = "";
                    CadDebugLog.Info($"[DrawingRevisionTracker] dirty sent session={sessionId} rev={revision} reason={reason}");
                }
                catch (TaskCanceledException) { }
                catch (Exception ex)
                {
                    CadDebugLog.Exception("DrawingRevisionTracker.MarkDirty", ex);
                }
            });
        }
    }
}
