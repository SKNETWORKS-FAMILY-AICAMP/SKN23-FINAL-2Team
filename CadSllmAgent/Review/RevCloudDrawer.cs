using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using CadSllmAgent.Models;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Review
{
    /// <summary>
    /// RES-03: 위반 entity 주변에 RevCloud 폴리라인 생성 (AI_REVIEW 레이어)
    /// RES-04: 구름마크 하단에 상세 내용(MText) 삽입. 겹치는 위반사항은 병합하여 리스트 형태로 출력.
    /// </summary>
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class RevCloudDrawer
    {
        public const string ReviewLayer    = "AI_REVIEW";
        public const string ReviewLayerLow = "AI_REVIEW_LOW";   // confidence < 0.7
        private const string XDataApp = "CADSLLM";

        private static readonly Dictionary<string, short> SeverityColor = new()
        {
            ["Critical"] = 1,
            ["Major"] = 30,
            ["Minor"] = 50,
        };

        public static void DrawAll(List<AnnotatedEntity> entities)
        {
            Document? doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null)
            {
                System.Windows.Forms.MessageBox.Show(
                    "열린 도면이 없습니다. AutoCAD에서 도면을 먼저 열어주세요.",
                    "CAD-Agent", System.Windows.Forms.MessageBoxButtons.OK,
                    System.Windows.Forms.MessageBoxIcon.Warning);
                return;
            }

            try
            {
                using var lockDoc = doc.LockDocument();
                using var tr = doc.Database.TransactionManager.StartTransaction();

                EnsureLayer(doc.Database, tr, ReviewLayer);
                EnsureLayer(doc.Database, tr, ReviewLayerLow);
                RegisterXDataApp(doc.Database, tr);

                var valid = new List<AnnotatedEntity>();
                foreach (var entity in entities)
                {
                    if (entity.Violation == null) continue;

                    if (!string.IsNullOrEmpty(entity.Handle))
                    {
                        try
                        {
                            long handleVal = Convert.ToInt64(entity.Handle, 16);
                            var objId = doc.Database.GetObjectId(false, new Handle(handleVal), 0);
                            if (objId != ObjectId.Null)
                            {
                                var ent = tr.GetObject(objId, OpenMode.ForRead) as Entity;
                                if (ent != null)
                                {
                                    var ext = ent.GeometricExtents;
                                    entity.BBox = new BoundingBox
                                    {
                                        X1 = ext.MinPoint.X,
                                        Y1 = ext.MinPoint.Y,
                                        X2 = ext.MaxPoint.X,
                                        Y2 = ext.MaxPoint.Y,
                                    };
                                    if (string.IsNullOrEmpty(entity.Layer))
                                        entity.Layer = ent.Layer;
                                }
                            }
                        }
                        catch (System.Exception bboxEx)
                        {
                            doc.Editor?.WriteMessage(
                                $"\n[RevCloud] handle {entity.Handle} BBox 추출 실패(무시): {bboxEx.Message}\n");
                        }
                    }

                    if (entity.BBox == null) continue;
                    valid.Add(entity);
                }

                int n = valid.Count;
                if (n > 0)
                {
                    var uf = new IntUnionFind(n);
                    for (int i = 0; i < n; i++)
                        for (int j = i + 1; j < n; j++)
                            if (BboxesOverlap(valid[i].BBox!, valid[j].BBox!))
                                uf.Union(i, j);

                    var buckets = new Dictionary<int, List<int>>();
                    for (int i = 0; i < n; i++)
                    {
                        int r = uf.Find(i);
                        if (!buckets.TryGetValue(r, out var list)) { list = new List<int>(); buckets[r] = list; }
                        list.Add(i);
                    }

                    foreach (var kv in buckets)
                    {
                        var cluster = kv.Value.Select(idx => valid[idx]).ToList();
                        DrawCluster(tr, doc.Database, cluster);
                    }
                }

                tr.Commit();
                doc.Editor.WriteMessage($"\n[RevCloud] 위반 항목 표시 완료 (AI_REVIEW 레이어)\n");
            }
            catch (System.Exception ex)
            {
                doc.Editor.WriteMessage($"\n[RevCloud 오류] {ex.Message}\n{ex.StackTrace}\n");
                System.Windows.Forms.MessageBox.Show(
                    $"RevCloud 오류:\n{ex.Message}\n\n{ex.StackTrace}",
                    "CAD-Agent Error", System.Windows.Forms.MessageBoxButtons.OK,
                    System.Windows.Forms.MessageBoxIcon.Error);
            }
        }

        public static void RemoveCloud(string violationId)
        {
            Document? doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;  // 도면 없으면 무시

            using var lockDoc = doc.LockDocument();
            using var tr = doc.Database.TransactionManager.StartTransaction();

            var bt = (BlockTable)tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

            var toErase = new List<ObjectId>();
            foreach (ObjectId id in btr)
            {
                var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                if (ent == null || ent.Layer != ReviewLayer) continue;

                var xd = ent.GetXDataForApplication(XDataApp);
                if (xd == null) continue;

                foreach (TypedValue tv in xd)
                {
                    if (tv.TypeCode == (int)DxfCode.ExtendedDataAsciiString &&
                        tv.Value?.ToString() == violationId)
                    {
                        toErase.Add(id);
                        break;
                    }
                }
            }
            foreach (var id in toErase)
                (tr.GetObject(id, OpenMode.ForWrite) as Entity)?.Erase();

            tr.Commit();
        }

        public static void RemoveAllCadSllmReviewMarks()
        {
            Document? doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            try
            {
                using var lockDoc = doc.LockDocument();
                using var tr = doc.Database.TransactionManager.StartTransaction();
                var bt = (BlockTable)tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead);
                var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

                var toErase = new List<ObjectId>();
                foreach (ObjectId id in btr)
                {
                    var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                    if (ent == null || ent.Layer != ReviewLayer) continue;
                    if (ent.GetXDataForApplication(XDataApp) == null) continue;
                    toErase.Add(id);
                }
                foreach (var oid in toErase)
                    (tr.GetObject(oid, OpenMode.ForWrite) as Entity)?.Erase();

                tr.Commit();
                doc.Editor.WriteMessage($"\n[RevCloud] 검토 마크 {toErase.Count}개 제거 (UI 리셋)\n");
            }
            catch (System.Exception ex)
            {
                doc.Editor.WriteMessage($"\n[RevCloud] RemoveAll: {ex.Message}\n");
            }
        }

        private static bool BboxesOverlap(BoundingBox a, BoundingBox b) =>
            a.X1 < b.X2 && a.X2 > b.X1 && a.Y1 < b.Y2 && a.Y2 > b.Y1;

        private static void DrawCluster(Transaction tr, Database db, List<AnnotatedEntity> cluster)
        {
            double minX = cluster.Min(e => e.BBox!.X1);
            double minY = cluster.Min(e => e.BBox!.Y1);
            double maxX = cluster.Max(e => e.BBox!.X2);
            double maxY = cluster.Max(e => e.BBox!.Y2);

            short colorIdx = 50;
            if (cluster.Any(e => e.Violation?.Severity == "Critical")) colorIdx = 1;
            else if (cluster.Any(e => e.Violation?.Severity == "Major")) colorIdx = 30;

            double margin = Math.Max((maxX - minX + maxY - minY) * 0.08, 10.0);
            double x1 = minX - margin, y1 = minY - margin;
            double x2 = maxX + margin, y2 = maxY + margin;

            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

            // confidence_score < 0.7 이면 LOW_CONF 레이어에 점선 처리
            bool isLowConf = cluster.All(e =>
                e.Violation != null &&
                e.Violation.ConfidenceScore.HasValue &&
                e.Violation.ConfidenceScore.Value < 0.7);
            string targetLayer = isLowConf ? ReviewLayerLow : ReviewLayer;

            var cloud = BuildCloudPolyline(x1, y1, x2, y2);
            cloud.Layer = targetLayer;
            cloud.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(ColorMethod.ByAci, colorIdx);
            if (isLowConf)
                cloud.Linetype = "DASHED";  // ACAD 기본 DASHED 선종류 (없으면 BYLAYER)
            btr.AppendEntity(cloud);
            tr.AddNewlyCreatedDBObject(cloud, true);

            var ids = cluster.Select(e => e.Violation?.Id)
                             .Where(id => !string.IsNullOrEmpty(id))
                             .Select(id => id!)
                             .Distinct()
                             .ToList();

            cloud.XData = MakeXData(ids);

            double cloudW = x2 - x1;
            double cloudH = y2 - y1;

            // 텍스트 기능 삭제 요청에 의해 주석 생성 및 추가 로직 주석 처리
            // var mtext = BuildClusterAnnotation(db, x1, y2, cluster, colorIdx, cloudW, cloudH);
            // btr.AppendEntity(mtext);
            // tr.AddNewlyCreatedDBObject(mtext, true);
            // mtext.Width  = Math.Min(Math.Max(cloudW * 0.95, 300.0), 1500.0);
            // mtext.Layer  = targetLayer;
            // mtext.XData  = MakeXData(ids);
        }

        internal static Polyline BuildCloudPolyline(double x1, double y1, double x2, double y2)
        {
            double w = x2 - x1, h = y2 - y1;
            double arcStep = Math.Max(Math.Min(w, h) / 4.0, 1.0);
            const double bulge = 0.5;

            var pts = new List<Point2d>();
            void AddSeg(double ax, double ay, double bx, double by)
            {
                double dx = bx - ax, dy = by - ay;
                double len = Math.Sqrt(dx * dx + dy * dy);
                int n = Math.Max(1, (int)(len / arcStep));
                for (int i = 0; i < n; i++)
                    pts.Add(new Point2d(ax + dx * i / n, ay + dy * i / n));
            }
            AddSeg(x1, y1, x2, y1);
            AddSeg(x2, y1, x2, y2);
            AddSeg(x2, y2, x1, y2);
            AddSeg(x1, y2, x1, y1);

            var pline = new Polyline();
            for (int i = 0; i < pts.Count; i++)
                pline.AddVertexAt(i, pts[i], bulge, 0, 0);
            pline.Closed = true;
            pline.ConstantWidth = Math.Clamp(Math.Min(w, h) * 0.005, 2.0, 15.0);
            return pline;
        }

        // x=구름x1, y=구름y2 (상단) — 내부 상단에 배치하여 인접 구름과 겹치지 않도록 함
        private static MText BuildClusterAnnotation(Database db, double x, double y, List<AnnotatedEntity> cluster, short colorIdx, double cloudW, double cloudH)
        {
            double textH = Math.Clamp(Math.Min(cloudW, cloudH) * 0.05, 2.5, 80.0);
            double padding = textH * 1.5;   // 구름 내부 여백
            double textWidth = Math.Min(Math.Max(cloudW * 0.9, 200.0), 800.0);

            var sb = new StringBuilder();
            if (cluster.Count == 1)
            {
                var v = cluster[0].Violation!;
                // 룰 이름: em대시(—) 앞까지만 추출 (설명 부분 제외)
                string rawRule = v.Rule.Contains('—') ? v.Rule.Split('—')[0].Trim() : v.Rule;
                string ruleName = rawRule.Length > 30 ? rawRule.Substring(0, 28) + ".." : rawRule;
                // \H1.1x = 1.1배 크기, \C7 = 흰색/검정 (기본색)
                sb.Append($"{{\\H1.1x;[위반] {ruleName}}}\\P");
                string desc = v.Description.Split('\n')[0];
                if (desc.Length > 45) desc = desc.Substring(0, 43) + "..";
                sb.Append($"{{\\C252;{desc}}}\\P");
                sb.Append("{\\C251;(웹 상세 확인)}");
            }
            else
            {
                sb.Append($"{{\\H1.2x;[복합 위반] {cluster.Count}건}}\\P");
                // 중복 룰명 → 통일 (횟수 표시)
                var ruleGroups = cluster
                    .Select(e => e.Violation!.Rule)
                    .GroupBy(r => r)
                    .OrderByDescending(g => g.Count())
                    .ToList();
                int lineIdx = 0;
                foreach (var grp in ruleGroups)
                {
                    // 룰 이름: em대시 앞까지만 추출
                    string rawRule = grp.Key.Contains('—') ? grp.Key.Split('—')[0].Trim() : grp.Key;
                    string ruleName = rawRule.Length > 28 ? rawRule.Substring(0, 26) + ".." : rawRule;
                    string countSuffix = grp.Count() > 1 ? $"  x{grp.Count()}" : "";
                    sb.Append($"{lineIdx + 1}. {ruleName}{countSuffix}\\P");
                    lineIdx++;
                    if (lineIdx >= 5) { sb.Append("{\\C251;+ 상세는 웹에서 확인}"); break; }
                }
            }

            // 구름 내부 상단 좌측 — y는 y2(구름 상단), TopLeft 기준으로 padding만큼 내려서 배치
            return new MText
            {
                Location = new Point3d(x + padding, y - padding, 0),
                TextHeight = textH,
                Width = textWidth,
                Contents = sb.ToString(),
                Layer = ReviewLayer,
                Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(ColorMethod.ByAci, colorIdx),
                Attachment = AttachmentPoint.TopLeft
            };
        }

        private static ResultBuffer MakeXData(IEnumerable<string> ids)
        {
            var rb = new ResultBuffer(new TypedValue((int)DxfCode.ExtendedDataRegAppName, XDataApp));
            foreach (var id in ids)
            {
                rb.Add(new TypedValue((int)DxfCode.ExtendedDataAsciiString, id));
            }
            return rb;
        }

        internal static void EnsureLayer(Database db, Transaction tr, string layerName)
        {
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            if (lt.Has(layerName)) return;
            lt.UpgradeOpen();
            var layer = new LayerTableRecord { Name = layerName };
            lt.Add(layer);
            tr.AddNewlyCreatedDBObject(layer, true);
        }

        private static void RegisterXDataApp(Database db, Transaction tr)
        {
            var rat = (RegAppTable)tr.GetObject(db.RegAppTableId, OpenMode.ForRead);
            if (rat.Has(XDataApp)) return;
            rat.UpgradeOpen();
            var app = new RegAppTableRecord { Name = XDataApp };
            rat.Add(app);
            tr.AddNewlyCreatedDBObject(app, true);
        }

        private sealed class IntUnionFind
        {
            private readonly int[] _p;
            public IntUnionFind(int n) { _p = new int[n]; for (int i = 0; i < n; i++) _p[i] = i; }
            public int Find(int i) { if (_p[i] != i) _p[i] = Find(_p[i]); return _p[i]; }
            public void Union(int a, int b)
            {
                a = Find(a); b = Find(b);
                if (a != b) { if (a < b) _p[b] = a; else _p[a] = b; }
            }
        }
    }
}