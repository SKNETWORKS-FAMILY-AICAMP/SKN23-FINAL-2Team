using System;
using System.Collections.Concurrent;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;
using Autodesk.AutoCAD.ApplicationServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class SafeTaskDispatcher
    {
        private static readonly ConcurrentQueue<Action> _pendingTasks = new();
        private static readonly object _stateLock = new();
        private static Document? _hookedDoc;
        private static Dispatcher? _uiDispatcher;
        private static int _drainScheduled;

        public static void CaptureUiDispatcher(Dispatcher? dispatcher = null)
        {
            dispatcher ??= System.Windows.Application.Current?.Dispatcher;
            if (dispatcher == null) return;
            _uiDispatcher = dispatcher;
        }

        public static void EnqueueSafeTask(Action action)
        {
            _pendingTasks.Enqueue(action);
            ScheduleDrain();
        }

        public static Task<T> EnqueueSafeTaskAsync<T>(Func<T> func)
        {
            var tcs = new TaskCompletionSource<T>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            _pendingTasks.Enqueue(() =>
            {
                try
                {
                    tcs.SetResult(func());
                }
                catch (Exception ex)
                {
                    tcs.SetException(ex);
                }
            });
            ScheduleDrain();
            return tcs.Task;
        }

        private static void ScheduleDrain()
        {
            if (Interlocked.Exchange(ref _drainScheduled, 1) == 1)
                return;

            var dispatcher = GetUiDispatcher();
            if (dispatcher != null && !dispatcher.CheckAccess())
            {
                dispatcher.BeginInvoke(
                    new Action(DrainOnUiDispatcher),
                    DispatcherPriority.ApplicationIdle);
                return;
            }

            DrainOnUiDispatcher();
        }

        private static Dispatcher? GetUiDispatcher()
        {
            var dispatcher = _uiDispatcher;
            if (dispatcher != null) return dispatcher;

            dispatcher = System.Windows.Application.Current?.Dispatcher;
            if (dispatcher != null)
                _uiDispatcher = dispatcher;

            return dispatcher;
        }

        private static void DrainOnUiDispatcher()
        {
            Interlocked.Exchange(ref _drainScheduled, 0);

            var dispatcher = GetUiDispatcher();
            if (dispatcher != null && !dispatcher.CheckAccess())
            {
                ScheduleDrain();
                return;
            }

            TryExecuteTasksOnUiDispatcher();
        }

        private static void TryExecuteTasksOnUiDispatcher()
        {
            try
            {
                AcApp.DocumentManager.ExecuteInApplicationContext(_ =>
                {
                    string cmdNames = AcApp.GetSystemVariable("CMDNAMES") as string ?? "";
                    if (!string.IsNullOrEmpty(cmdNames))
                    {
                        // A command is active. We must wait until it finishes.
                        EnsureHooked();
                        return;
                    }

                    // No command active. Safe to execute.
                    UnhookCommandEvents();
                    ExecuteAllPending();
                }, null);
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("SafeTaskDispatcher.TryExecuteTasks", ex);
            }
        }

        private static void ExecuteAllPending()
        {
            while (_pendingTasks.TryDequeue(out var action))
            {
                try
                {
                    action();
                }
                catch (Exception ex)
                {
                    CadDebugLog.Exception("SafeTaskDispatcher task execution", ex);
                }
            }
        }

        private static void EnsureHooked()
        {
            lock (_stateLock)
            {
                var doc = AcApp.DocumentManager.MdiActiveDocument;
                if (doc == null) return;
                if (ReferenceEquals(_hookedDoc, doc)) return;

                UnhookCommandEventsLocked();
                doc.CommandEnded += OnCommandEnded;
                doc.CommandCancelled += OnCommandCancelled;
                doc.CommandFailed += OnCommandFailed;
                _hookedDoc = doc;
            }
        }

        private static void UnhookCommandEvents()
        {
            lock (_stateLock)
            {
                UnhookCommandEventsLocked();
            }
        }

        private static void UnhookCommandEventsLocked()
        {
            var doc = _hookedDoc;
            _hookedDoc = null;
            if (doc == null) return;

            try { doc.CommandEnded -= OnCommandEnded; } catch { }
            try { doc.CommandCancelled -= OnCommandCancelled; } catch { }
            try { doc.CommandFailed -= OnCommandFailed; } catch { }
        }

        private static void OnCommandEnded(object sender, CommandEventArgs e) => ScheduleDrain();
        private static void OnCommandCancelled(object sender, CommandEventArgs e) => ScheduleDrain();
        private static void OnCommandFailed(object sender, CommandEventArgs e) => ScheduleDrain();
    }
}
