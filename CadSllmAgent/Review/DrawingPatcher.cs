/*
 * File    : CadSllmAgent/Review/DrawingPatcher.cs
 * Author  : 김다빈
 * WBS     : RES-04 (도면 직접 수정)
 * Create  : 2026-04-06
 *
 * Description :
 * 사용자가 위반 항목을 승인(Approve)하면 auto_fix 정보를 바탕으로
 * AutoCAD 도면 entity를 직접 수정하거나 새로 생성한다.
 *
 * 진입점: ApplyFix(AnnotatedEntity)
 * 1. auto_fix.type 확인 (CREATE 계열이면 핸들 검사 우회)
 * 2. entity.Handle → ObjectId 변환 (기존 객체 수정 시)
 * 3. auto_fix.type에 따라 수정/생성 메서드 분기
 * 4. 성공 시 RevCloudDrawer.RemoveCloud()로 구름 마크 제거
 *
 * 지원 fix type:
 * [수정 계열]
 * ATTRIBUTE    — 블록 속성값 변경
 * LAYER        — 레이어 변경
 * TEXT_CONTENT — MText / DBText 내용 변경
 * TEXT_HEIGHT  — 글자 크기 변경
 * COLOR        — ACI 색상 변경
 * LINETYPE     — 선종류 변경
 * LINEWEIGHT   — 선두께 변경 (mm → AutoCAD LineWeight enum 근사)
 * DELETE       — entity 삭제
 * MOVE         — 상대 이동 (delta_x, delta_y)
 * ROTATE       — 회전 (angle°, base_x, base_y)
 * SCALE        — 배율 조정 (scale_x, scale_y, base_x, base_y)
 * GEOMETRY     — 좌표 직접 수정
 * BLOCK_REPLACE — 블록 정의만 교체(삽입점 유지)
 * DYNAMIC_BLOCK_PARAM — 동적 블록 파라미터 반영
 *
 * [생성 계열 - NEW!]
 * CREATE_ENTITY (또는 CREATE_...)
 * - 선, 원, 폴리선(다각형/직사각형), 블록 모형 신규 추가 지원
 */

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Colors;
using CadSllmAgent.Models;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;
using AcColor = Autodesk.AutoCAD.Colors.Color;

namespace CadSllmAgent.Review
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class DrawingPatcher
    {
        public const string ProposalLayerName = "AI_PROPOSAL";

        /// <summary>
        /// 사용자 채팅 명령(CAD_ACTION)에 의한 신규 객체 직접 생성.
        /// ApplyFix()와 달리 기존 AnnotatedEntity / 위반 ID가 없어도 동작한다.
        /// 성공 시 true, 좌표·블록명 부족 또는 오류 시 false.
        /// </summary>
        public static bool CreateEntity(AutoFix fix)
        {
            if (fix == null) return false;

            var doc = AcApp.DocumentManager.MdiActiveDocument;
            using var lockDoc = doc.LockDocument();
            using var tr = doc.Database.TransactionManager.StartTransaction();
            try
            {
                bool ok = ApplyCreateEntity(doc, tr, fix);
                if (ok) tr.Commit(); else tr.Abort();
                return ok;
            }
            catch (Exception ex)
            {
                doc.Editor?.WriteMessage($"\n[DrawingPatcher] CreateEntity 오류: {ex.Message}\n");
                tr.Abort();
                return false;
            }
        }

        /// <summary>
        /// entity의 auto_fix를 도면에 적용한다.
        /// modification_tier 4 — 삭제/이동/기하/배율(위험)은 원본 유지, AI_PROPOSAL에 제안.
        /// 성공 시 true + RevCloud 제거. 핸들 미발견·fix 없으면 false.
        /// </summary>
        public static bool ApplyFix(AnnotatedEntity entity)
        {
            if (entity.Violation?.AutoFix == null) return false;

            var doc = AcApp.DocumentManager.MdiActiveDocument;
            using var lockDoc = doc.LockDocument();
            using var tr = doc.Database.TransactionManager.StartTransaction();

            try
            {
                var fix = entity.Violation.AutoFix;
                int tier = ResolveModificationTier(entity);
                string ftype = (fix.Type ?? "").Trim().ToUpperInvariant();

                // ✨ [수정됨] "CREATE"로 시작하는 명령은 기존 핸들이 없어도(참조용이어도) 통과시킴
                bool isCreateCommand = ftype.StartsWith("CREATE");
                bool hasValidObj = TryGetObjectId(doc.Database, entity.Handle, out ObjectId objId);

                if (!hasValidObj && !isCreateCommand)
                {
                    doc.Editor.WriteMessage($"\n[DrawingPatcher] handle {entity.Handle} 미발견\n");
                    tr.Abort();
                    return false;
                }

                // 4단계 위험도 처리 (생성 명령은 원본이 없으므로 제외)
                if (tier >= 4 && !isCreateCommand)
                {
                    bool ok4 = ApplyDangerZoneAsProposal(doc, tr, objId, entity, fix, tier);
                    if (ok4)
                    {
                        tr.Commit();
                        RevCloudDrawer.RemoveCloud(entity.Violation.Id);
                    }
                    else
                        tr.Abort();
                    return ok4;
                }

                if (string.Equals(fix.Type, "DELETE", StringComparison.OrdinalIgnoreCase))
                {
                    if (hasValidObj)
                    {
                        var target = tr.GetObject(objId, OpenMode.ForWrite) as Entity;
                        target?.Erase();
                    }
                    tr.Commit();
                    RevCloudDrawer.RemoveCloud(entity.Violation.Id);
                    return true;
                }

                // ✨ Nullable 참조 형식(?) 적용
                DBObject? dbObj = hasValidObj ? tr.GetObject(objId, OpenMode.ForWrite) : null;

                doc.Editor.WriteMessage($"\n[CAD-Agent Debug] 타입: {ftype}, 받은 레이어명: '{fix.NewLayer ?? "NULL"}'\n");

                bool ok = ApplyByType(doc, tr, dbObj, fix);

                if (ok)
                {
                    tr.Commit();
                    RevCloudDrawer.RemoveCloud(entity.Violation.Id);
                }
                else
                {
                    tr.Abort();
                }

                return ok;
            }
            catch (Exception ex)
            {
                doc.Editor.WriteMessage($"\n[DrawingPatcher] 수정/생성 오류: {ex.Message}\n");
                tr.Abort();
                return false;
            }
        }

        private static int ResolveModificationTier(AnnotatedEntity entity)
        {
            int t = entity.Violation?.ModificationTier ?? 0;
            if (t <= 0)
                t = entity.Violation?.AutoFix?.ModificationTier ?? 0;
            return t <= 0 ? 1 : t;
        }

        /// <summary>4단계: 원본을 바꾸지 않고 AI_PROPOSAL에 복제 후 수정(또는 삭제/기하 제안 MText)</summary>
        private static bool ApplyDangerZoneAsProposal(
            Document doc, Transaction tr, ObjectId sourceId, AnnotatedEntity entity, AutoFix fix, int tier)
        {
            if (string.Equals(fix.Type, "DELETE", StringComparison.OrdinalIgnoreCase))
                return AddProposalDeleteNote(doc, tr, sourceId, entity, fix);

            var readObj = tr.GetObject(sourceId, OpenMode.ForRead) as Entity;
            if (readObj == null) return false;
            var clone = (Entity)readObj.Clone();
            if (clone == null) return false;
            try
            {
                EnsureLayerExists(doc.Database, tr, ProposalLayerName);
            }
            catch
            { /* best-effort */ }
            var bt = (BlockTable)tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
            btr.AppendEntity(clone);
            tr.AddNewlyCreatedDBObject(clone, true);
            clone.Layer = ProposalLayerName;
            clone.Color = AcColor.FromColorIndex(ColorMethod.ByAci, 1);

            if (!ApplyByType(doc, tr, clone, fix))
            {
                return false;
            }
            doc.Editor.WriteMessage(
                $"\n[DrawingPatcher] 4단계: 원본은 유지, {ProposalLayerName}에 수정 제안을 복제했습니다.\n");
            return true;
        }

        private static bool AddProposalDeleteNote(
            Document doc, Transaction tr, ObjectId sourceId, AnnotatedEntity entity, AutoFix fix)
        {
            EnsureLayerExists(doc.Database, tr, ProposalLayerName);
            if (!(tr.GetObject(sourceId, OpenMode.ForRead) is Entity ent)) return false;
            var p = new Point3d(0, 0, 0);
            try
            {
                var ext = ent.GeometricExtents;
                p = new Point3d(
                    0.5 * (ext.MinPoint.X + ext.MaxPoint.X),
                    0.5 * (ext.MinPoint.Y + ext.MaxPoint.Y), 0);
            }
            catch
            { /* ignore */ }
            var m = new MText
            {
                Location = p,
                Layer = ProposalLayerName,
                TextHeight = 150.0,
                Color = AcColor.FromColorIndex(ColorMethod.ByAci, 1),
                Contents = $"[AI 삭제 제안]\\P원본(handle={entity.Handle})은 유지됨.\\P수동으로 삭제 여부를 결정하세요.\\P(violation {entity.Violation?.Id})",
                Width = 2000.0,
            };
            var bt = (BlockTable)tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
            btr.AppendEntity(m);
            tr.AddNewlyCreatedDBObject(m, true);
            doc.Editor.WriteMessage("\n[DrawingPatcher] 4단계(삭제): 원본 유지, AI_PROPOSAL에 삭제 제안 MText\n");
            return true;
        }

        // ── fix type 분기 ─────────────────────────────────────────────────

        // ✨ Nullable 참조 형식(?) 적용
        private static bool ApplyByType(Document doc, Transaction tr, DBObject? dbObj, AutoFix fix)
        {
            var ft = (fix.Type ?? "").Trim().ToUpperInvariant();

            if (ft.StartsWith("CREATE"))
            {
                return ApplyCreateEntity(doc, tr, fix);
            }

            // 기존 객체가 null이면 아래 수정 작업들은 수행할 수 없음
            if (dbObj == null) return false;

            switch (ft)
            {
                case "ATTRIBUTE":
                    return ApplyAttribute(tr, dbObj, fix);

                case "LAYER":
                    if (dbObj is Entity le && fix.NewLayer != null)
                    {
                        EnsureLayerExists(doc.Database, tr, fix.NewLayer);
                        le.Layer = fix.NewLayer;
                        return true;
                    }
                    return false;

                case "TEXT_CONTENT":
                    return ApplyTextContent(dbObj, fix);

                case "TEXT_HEIGHT":
                    return ApplyTextHeight(dbObj, fix);

                case "COLOR":
                    return ApplyColor(dbObj, fix);

                case "LINETYPE":
                    return ApplyLinetype(dbObj, fix);

                case "LINEWEIGHT":
                    return ApplyLineweight(dbObj, fix);

                case "MOVE":
                    return ApplyMove(dbObj, fix);

                case "ROTATE":
                    return ApplyRotate(dbObj, fix);

                case "SCALE":
                    return ApplyScale(dbObj, fix);

                case "GEOMETRY":
                    return ApplyGeometry(dbObj, fix);

                case "RECTANGLE_RESIZE":
                case "STRETCH_RECT":
                    return ApplyRectangleResize(dbObj, fix);

                case "BLOCK_REPLACE":
                    return ApplyBlockReplace(tr, dbObj, fix);

                case "DYNAMIC_BLOCK_PARAM":
                    return ApplyDynamicBlockParam(dbObj, fix, doc);

                default:
                    doc.Editor.WriteMessage($"\n[DrawingPatcher] 알 수 없는 fix type: {ft}\n");
                    return false;
            }
        }

        private static bool ApplyCreateEntity(Document doc, Transaction tr, AutoFix fix)
        {
            // ✨ Nullable 참조 형식(?) 적용
            Entity? newEnt = null;
            string targetLayer = fix.NewLayer ?? ProposalLayerName;

            try
            {
                // 1. 폴리선(직사각형, 삼각형, 자유다각형) 생성
                if (fix.NewVertices != null && fix.NewVertices.Count > 0)
                {
                    var pl = new Polyline(fix.NewVertices.Count);
                    for (int i = 0; i < fix.NewVertices.Count; i++)
                    {
                        var v = fix.NewVertices[i];
                        pl.AddVertexAt(i, new Point2d(v.X, v.Y), v.Bulge, 0, 0);
                    }
                    pl.Closed = true;
                    newEnt = pl;
                }
                // 2. 원(Circle) 생성
                else if (fix.NewCenter != null && fix.NewRadius != null)
                {
                    newEnt = new Circle(new Point3d(fix.NewCenter.X, fix.NewCenter.Y, 0), Vector3d.ZAxis, fix.NewRadius.Value);
                }
                // 3. 선(Line) 생성
                else if (fix.NewStart != null && fix.NewEnd != null)
                {
                    newEnt = new Line(new Point3d(fix.NewStart.X, fix.NewStart.Y, 0), new Point3d(fix.NewEnd.X, fix.NewEnd.Y, 0));
                }
                // 4. 모형(Block) 추가
                else if (!string.IsNullOrWhiteSpace(fix.NewText))
                {
                    newEnt = new DBText
                    {
                        Position = new Point3d(fix.BaseX ?? 0, fix.BaseY ?? 0, 0),
                        TextString = fix.NewText ?? "",
                        Height = fix.NewHeight ?? 150.0,
                    };
                }
                else if (!string.IsNullOrWhiteSpace(fix.NewBlockName))
                {
                    var bt = (BlockTable)tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead);
                    if (bt.Has(fix.NewBlockName))
                    {
                        ObjectId btrId = bt[fix.NewBlockName];
                        newEnt = new BlockReference(new Point3d(fix.BaseX ?? 0, fix.BaseY ?? 0, 0), btrId);
                    }
                    else
                    {
                        doc.Editor.WriteMessage($"\n[DrawingPatcher] 블록 '{fix.NewBlockName}'을(를) 도면에서 찾을 수 없습니다.\n");
                        return false;
                    }
                }

                if (newEnt != null)
                {
                    EnsureLayerExists(doc.Database, tr, targetLayer);
                    newEnt.Layer = targetLayer;

                    BlockTable bt = (BlockTable)tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead);
                    BlockTableRecord ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

                    ms.AppendEntity(newEnt);
                    tr.AddNewlyCreatedDBObject(newEnt, true);

                    doc.Editor.WriteMessage($"\n[DrawingPatcher] 신규 객체 생성 성공 (타입: {newEnt.GetType().Name}, 레이어: {targetLayer})\n");
                    return true;
                }

                doc.Editor.WriteMessage("\n[DrawingPatcher] 신규 객체 생성을 위한 좌표나 데이터가 충분하지 않습니다.\n");
                return false;
            }
            catch (Exception ex)
            {
                doc.Editor.WriteMessage($"\n[DrawingPatcher] 신규 객체 생성 중 오류: {ex.Message}\n");
                return false;
            }
        }

        // ── 기존 각 fix type 구현 ───────────────────────────────────────────────

        private static bool ApplyBlockReplace(Transaction tr, DBObject obj, AutoFix fix)
        {
            if (obj is not BlockReference br || string.IsNullOrWhiteSpace(fix.NewBlockName)) return false;
            try
            {
                var db = br.Database;
                if (db == null) return false;
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                string n = fix.NewBlockName.Trim();
                if (!bt.Has(n)) return false;
                ObjectId newBtrId = bt[n];
                if (newBtrId.IsNull) return false;

                ObjectId ownerBtrId = br.OwnerId;
                string layer = br.Layer;
                var pos = br.Position;
                var rot = br.Rotation;
                var sc = br.ScaleFactors;
                var normal = br.Normal;

                br.UpgradeOpen();
                br.Erase();

                var newBr = new BlockReference(pos, newBtrId)
                {
                    Layer = layer,
                    Rotation = rot,
                    ScaleFactors = sc,
                    Normal = normal,
                };
                var space = (BlockTableRecord)tr.GetObject(ownerBtrId, OpenMode.ForWrite);
                space.AppendEntity(newBr);
                tr.AddNewlyCreatedDBObject(newBr, true);
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static bool ApplyDynamicBlockParam(DBObject obj, AutoFix fix, Document doc)
        {
            if (obj is not BlockReference br) return false;

            string? wantName = fix.ParamName ?? fix.ParameterName;
            if (string.IsNullOrWhiteSpace(wantName))
            {
                doc.Editor.WriteMessage("\n[DrawingPatcher] 3단계: param_name(또는 parameter_name)이 비어 있습니다.\n");
                return false;
            }

            if (!br.IsDynamicBlock)
            {
                doc.Editor.WriteMessage($"\n[DrawingPatcher] 3단계: 동적 블록이 아닙니다 (handle {br.Handle}).\n");
                return false;
            }

            string raw = fix.ParamValue ?? fix.ValueParam ?? "";
            var ed = doc.Editor;

            try
            {
                var coll = br.DynamicBlockReferencePropertyCollection;
                if (coll == null)
                {
                    ed.WriteMessage("\n[DrawingPatcher] 3단계: DynamicBlockReferencePropertyCollection이 없습니다.\n");
                    return false;
                }
                var hasProp = false;
                foreach (DynamicBlockReferenceProperty _ in coll)
                {
                    hasProp = true;
                    break;
                }
                if (!hasProp)
                {
                    ed.WriteMessage("\n[DrawingPatcher] 3단계: 동적 파라미터 목록이 비어 있습니다.\n");
                    return false;
                }

                foreach (DynamicBlockReferenceProperty prop in coll)
                {
                    if (!DynamicParamNameMatches(wantName, prop)) continue;
                    if (prop.ReadOnly)
                    {
                        ed.WriteMessage($"\n[DrawingPatcher] 3단계: '{prop.PropertyName}' 은(는) 읽기 전용입니다.\n");
                        return false;
                    }

                    try
                    {
                        object newVal = CoerceDynamicParamValue(prop, raw);
                        prop.Value = newVal;
                        ed.WriteMessage(
                            $"\n[DrawingPatcher] 3단계: 동적 파라미터 적용 → {prop.PropertyName} = {newVal}\n");
                        return true;
                    }
                    catch (Exception ex)
                    {
                        ed.WriteMessage(
                            $"\n[DrawingPatcher] 3단계: '{prop.PropertyName}' 값 설정 실패: {ex.Message}\n");
                        return false;
                    }
                }

                ed.WriteMessage(
                    $"\n[DrawingPatcher] 3단계: '{wantName}' 과 일치하는 파라미터를 찾지 못했습니다. " +
                    $"사용 가능: {FormatDynamicPropNames(coll)}\n");
                return false;
            }
            catch (Exception ex)
            {
                doc.Editor.WriteMessage($"\n[DrawingPatcher] 3단계(동적 블록) 오류: {ex.Message}\n");
                return false;
            }
        }

        private static bool DynamicParamNameMatches(string want, DynamicBlockReferenceProperty prop)
        {
            if (NameKeyEquals(want, prop.PropertyName)) return true;
            try
            {
                if (!string.IsNullOrEmpty(prop.Description) && NameKeyEquals(want, prop.Description))
                    return true;
            }
            catch { /* Description 없을 수 있음 */ }
            return false;
        }

        private static bool NameKeyEquals(string a, string b)
        {
            if (string.IsNullOrEmpty(a) || string.IsNullOrEmpty(b)) return false;
            if (string.Equals(a.Trim(), b.Trim(), StringComparison.OrdinalIgnoreCase)) return true;
            string na = new string(a.Where(c => !char.IsWhiteSpace(c) && c != '_').ToArray());
            string nb = new string(b.Where(c => !char.IsWhiteSpace(c) && c != '_').ToArray());
            return string.Equals(na, nb, StringComparison.OrdinalIgnoreCase);
        }

        private static string FormatDynamicPropNames(DynamicBlockReferencePropertyCollection coll)
        {
            var parts = new List<string>();
            foreach (DynamicBlockReferenceProperty p in coll)
            {
                if (p == null) continue;
                try
                {
                    parts.Add(string.IsNullOrEmpty(p.PropertyName) ? "?" : p.PropertyName);
                }
                catch { /* ignore */ }
                if (parts.Count >= 12) break;
            }
            return parts.Count == 0 ? "(없음)" : string.Join(", ", parts);
        }

        private static object CoerceDynamicParamValue(DynamicBlockReferenceProperty prop, string raw)
        {
            object? cur = null;
            try { cur = prop.Value; } catch { /* ignore */ }

            if (cur != null)
            {
                switch (cur)
                {
                    case string s0:
                        return MatchOrUseStringParam(cur, raw, s0);
                    case bool:
                        if (bool.TryParse(raw, out var bl)) return bl;
                        if (string.Equals(raw, "1", StringComparison.Ordinal)) return true;
                        if (string.Equals(raw, "0", StringComparison.Ordinal)) return false;
                        throw new InvalidOperationException($"bool 변환 불가: '{raw}'");
                    case double:
                    case float:
                        if (TryParseDoubleLocale(raw, out var d)) return d;
                        throw new InvalidOperationException($"실수 변환 불가: '{raw}'");
                    case decimal:
                        if (decimal.TryParse(raw.Trim(), NumberStyles.Any, CultureInfo.InvariantCulture, out var dec)) return dec;
                        if (decimal.TryParse(raw.Trim(), NumberStyles.Any, CultureInfo.CurrentCulture, out dec)) return dec;
                        throw new InvalidOperationException($"decimal 변환 불가: '{raw}'");
                    case int:
                    case short:
                    case long:
                    case byte:
                    case uint:
                    case ushort:
                    case ulong:
                        if (TryParseDoubleLocale(raw, out var d2))
                        {
                            try { return Convert.ChangeType((int)Math.Round(d2), cur.GetType(), CultureInfo.InvariantCulture); }
                            catch { /* fall through */ }
                        }
                        if (long.TryParse(raw.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var li))
                            return Convert.ChangeType(li, cur.GetType(), CultureInfo.InvariantCulture);
                        throw new InvalidOperationException($"정수 변환 불가: '{raw}'");
                    default:
                        {
                            var t = cur.GetType();
                            if (t.IsEnum)
                            {
                                try { return Enum.Parse(t, raw, true); }
                                catch (Exception ex)
                                {
                                    throw new InvalidOperationException($"열거형 {t.Name} 파싱 불가: '{raw}'", ex);
                                }
                            }
                            if (cur is IConvertible && cur is not string)
                            {
                                if (TryParseDoubleLocale(raw, out var d4))
                                {
                                    try
                                    {
                                        return Convert.ChangeType(d4, cur.GetType(), CultureInfo.InvariantCulture);
                                    }
                                    catch { /* ignore */ }
                                }
                            }
                            break;
                        }
                }
            }

            if (bool.TryParse(raw, out var b2)) return b2;
            if (long.TryParse(raw.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var li2)) return (int)li2;
            if (TryParseDoubleLocale(raw, out var d3)) return d3;
            return raw;
        }

        private static object MatchOrUseStringParam(object cur, string raw, string s0)
        {
            string s = raw ?? "";
            if (string.Equals(s0, s, StringComparison.Ordinal)) return cur;
            if (string.Equals(s0, s, StringComparison.OrdinalIgnoreCase)) return s;
            return s;
        }

        private static bool TryParseDoubleLocale(string raw, out double value)
        {
            value = 0;
            if (string.IsNullOrWhiteSpace(raw)) return false;
            string t = raw.Trim();
            if (double.TryParse(t, NumberStyles.Float, CultureInfo.InvariantCulture, out value)) return true;
            if (double.TryParse(t, NumberStyles.Float, CultureInfo.CurrentCulture, out value)) return true;
            try
            {
                if (double.TryParse(t, NumberStyles.Float, CultureInfo.GetCultureInfo("ko-KR"), out value)) return true;
            }
            catch { /* ignore */ }
            return false;
        }

        private static bool ApplyAttribute(Transaction tr, DBObject obj, AutoFix fix)
        {
            if (obj is not BlockReference bref || fix.AttributeTag == null) return false;
            foreach (ObjectId attId in bref.AttributeCollection)
            {
                var att = (AttributeReference)tr.GetObject(attId, OpenMode.ForWrite);
                if (att.Tag.Equals(fix.AttributeTag, StringComparison.OrdinalIgnoreCase))
                {
                    att.TextString = fix.NewValue ?? "";
                    return true;
                }
            }
            return false;
        }

        private static bool ApplyTextContent(DBObject obj, AutoFix fix)
        {
            if (fix.NewText == null) return false;
            if (obj is MText mt) { mt.Contents = fix.NewText; return true; }
            else if (obj is DBText dt) { dt.TextString = fix.NewText; return true; }
            return false;
        }

        private static bool ApplyTextHeight(DBObject obj, AutoFix fix)
        {
            if (fix.NewHeight == null) return false;
            if (obj is MText mt) { mt.TextHeight = fix.NewHeight.Value; return true; }
            else if (obj is DBText dt) { dt.Height = fix.NewHeight.Value; return true; }
            return false;
        }

        private static bool ApplyColor(DBObject obj, AutoFix fix)
        {
            if (obj is not Entity ent || fix.NewColor == null) return false;
            int aci = fix.NewColor.Value;
            if (aci < 0 || aci > 257) return false;
            ent.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(
                Autodesk.AutoCAD.Colors.ColorMethod.ByAci, (short)aci);
            return true;
        }

        private static bool ApplyLinetype(DBObject obj, AutoFix fix)
        {
            if (obj is not Entity ent || fix.NewLinetype == null) return false;
            try { ent.Linetype = fix.NewLinetype; return true; }
            catch { return false; }
        }

        private static bool ApplyLineweight(DBObject obj, AutoFix fix)
        {
            if (obj is not Entity ent || fix.NewLineweight == null) return false;

            int[] validLW = { 0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40,
                              50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211 };
            int target = (int)Math.Round(fix.NewLineweight.Value * 100);
            int nearest = validLW.OrderBy(v => Math.Abs(v - target)).First();

            ent.LineWeight = (LineWeight)nearest;
            return true;
        }

        private static bool ApplyMove(DBObject obj, AutoFix fix)
        {
            if (obj is not Entity ent) return false;
            double dx = fix.DeltaX ?? 0;
            double dy = fix.DeltaY ?? 0;
            ent.TransformBy(Matrix3d.Displacement(new Vector3d(dx, dy, 0)));
            return true;
        }

        private static bool ApplyRotate(DBObject obj, AutoFix fix)
        {
            if (obj is not Entity ent || fix.Angle == null) return false;
            double rad = fix.Angle.Value * Math.PI / 180.0;
            var base_ = new Point3d(fix.BaseX ?? 0, fix.BaseY ?? 0, 0);
            ent.TransformBy(Matrix3d.Rotation(rad, Vector3d.ZAxis, base_));
            return true;
        }

        private static bool ApplyScale(DBObject obj, AutoFix fix)
        {
            if (fix.ScaleX == null) return false;

            if (obj is BlockReference bref)
            {
                double sx = fix.ScaleX.Value;
                double sy = fix.ScaleY ?? sx;
                bref.ScaleFactors = new Scale3d(sx, sy, 1.0);
                return true;
            }

            if (obj is Entity ent)
            {
                var base_ = new Point3d(fix.BaseX ?? 0, fix.BaseY ?? 0, 0);
                ent.TransformBy(Matrix3d.Scaling(fix.ScaleX.Value, base_));
                return true;
            }

            return false;
        }

        private static bool ApplyRectangleResize(DBObject obj, AutoFix fix)
        {
            if (obj is not Polyline pl || pl.NumberOfVertices < 4) return false;

            var xs = new List<double>();
            var ys = new List<double>();
            for (int i = 0; i < pl.NumberOfVertices; i++)
            {
                Point2d p = pl.GetPoint2dAt(i);
                xs.Add(p.X);
                ys.Add(p.Y);
            }

            double minX = xs.Min();
            double maxX = xs.Max();
            double minY = ys.Min();
            double maxY = ys.Max();
            double oldW = maxX - minX;
            double oldH = maxY - minY;
            if (oldW <= 0 || oldH <= 0) return false;

            string side = (fix.StretchSide ?? "right").Trim().ToLowerInvariant();
            double targetMinX = minX;
            double targetMaxX = maxX;
            double targetMinY = minY;
            double targetMaxY = maxY;

            if (fix.NewWidth.HasValue && fix.NewWidth.Value > 0)
            {
                if (side == "left")
                    targetMinX = maxX - fix.NewWidth.Value;
                else
                    targetMaxX = minX + fix.NewWidth.Value;
            }
            else if (fix.DeltaX.HasValue && Math.Abs(fix.DeltaX.Value) > 0)
            {
                if (side == "left")
                    targetMinX = minX + fix.DeltaX.Value;
                else
                    targetMaxX = maxX + fix.DeltaX.Value;
            }

            if (fix.NewHeight.HasValue && fix.NewHeight.Value > 0)
            {
                if (side == "bottom")
                    targetMinY = maxY - fix.NewHeight.Value;
                else
                    targetMaxY = minY + fix.NewHeight.Value;
            }
            else if (fix.DeltaY.HasValue && Math.Abs(fix.DeltaY.Value) > 0)
            {
                if (side == "bottom")
                    targetMinY = minY + fix.DeltaY.Value;
                else
                    targetMaxY = maxY + fix.DeltaY.Value;
            }

            bool changed = Math.Abs(targetMinX - minX) > 1e-9
                        || Math.Abs(targetMaxX - maxX) > 1e-9
                        || Math.Abs(targetMinY - minY) > 1e-9
                        || Math.Abs(targetMaxY - maxY) > 1e-9;
            if (!changed) return false;

            for (int i = 0; i < pl.NumberOfVertices; i++)
            {
                Point2d p = pl.GetPoint2dAt(i);
                double x = p.X;
                double y = p.Y;

                if (Math.Abs(p.X - minX) <= 1e-6) x = targetMinX;
                else if (Math.Abs(p.X - maxX) <= 1e-6) x = targetMaxX;

                if (Math.Abs(p.Y - minY) <= 1e-6) y = targetMinY;
                else if (Math.Abs(p.Y - maxY) <= 1e-6) y = targetMaxY;

                pl.SetPointAt(i, new Point2d(x, y));
            }

            return true;
        }

        private static bool ApplyGeometry(DBObject obj, AutoFix fix)
        {
            switch (obj)
            {
                case Line line:
                    if (fix.NewStart != null)
                        line.StartPoint = new Point3d(fix.NewStart.X, fix.NewStart.Y, 0);
                    if (fix.NewEnd != null)
                        line.EndPoint = new Point3d(fix.NewEnd.X, fix.NewEnd.Y, 0);
                    return fix.NewStart != null || fix.NewEnd != null;

                case Circle circle:
                    if (fix.NewCenter != null)
                        circle.Center = new Point3d(fix.NewCenter.X, fix.NewCenter.Y, 0);
                    if (fix.NewRadius != null)
                        circle.Radius = fix.NewRadius.Value;
                    return fix.NewCenter != null || fix.NewRadius != null;

                case Arc arc:
                    if (fix.NewCenter != null)
                        arc.Center = new Point3d(fix.NewCenter.X, fix.NewCenter.Y, 0);
                    if (fix.NewRadius != null)
                        arc.Radius = fix.NewRadius.Value;
                    if (fix.NewStartAngle != null)
                        arc.StartAngle = fix.NewStartAngle.Value * Math.PI / 180.0;
                    if (fix.NewEndAngle != null)
                        arc.EndAngle = fix.NewEndAngle.Value * Math.PI / 180.0;
                    return true;

                case Polyline pl:
                    if (fix.NewVertices == null) return false;
                    int count = Math.Min(pl.NumberOfVertices, fix.NewVertices.Count);
                    for (int i = 0; i < count; i++)
                    {
                        var v = fix.NewVertices[i];
                        pl.SetPointAt(i, new Point2d(v.X, v.Y));
                        pl.SetBulgeAt(i, v.Bulge);
                    }
                    return count > 0;

                default:
                    return false;
            }
        }

        private static bool TryGetObjectId(Database db, string handleStr, out ObjectId objId)
        {
            objId = ObjectId.Null;
            if (string.IsNullOrWhiteSpace(handleStr)) return false;
            var s = handleStr.Trim();
            if (s.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
                s = s[2..];
            try
            {
                long h16 = Convert.ToInt64(s, 16);
                var h = new Handle(h16);
                if (db.TryGetObjectId(h, out objId) && !objId.IsNull)
                    return true;
            }
            catch { /* hex 실패 */ }
            try
            {
                if (long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out long d))
                {
                    var h2 = new Handle(d);
                    return db.TryGetObjectId(h2, out objId) && !objId.IsNull;
                }
            }
            catch { /* ignore */ }
            return false;
        }

        /// <summary>도면에 레이어가 없으면 자동 생성. LAYER fix 및 신규 생성 적용 전 반드시 호출.</summary>
        private static void EnsureLayerExists(Database db, Transaction tr, string layerName)
        {
            if (string.IsNullOrWhiteSpace(layerName)) return;
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            if (lt.Has(layerName)) return;
            lt.UpgradeOpen();
            var layer = new LayerTableRecord { Name = layerName };
            lt.Add(layer);
            tr.AddNewlyCreatedDBObject(layer, true);
        }
    }
}
