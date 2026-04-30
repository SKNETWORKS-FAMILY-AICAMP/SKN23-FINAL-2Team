/*
 * File    : CadSllmAgent/Review/ReviewModels.cs
 * Author  : 김다빈
 * WBS     : RES-03 (RevCloud), RES-04 (DrawingPatcher)
 * Create  : 2026-04-06
 *
 * Description :
 *   Python ChatResponse.annotated_entities 를 C#이 받아서 처리하는 데이터 모델.
 *
 *   흐름:
 *     Python → React → C# REVIEW_RESULT
 *       AnnotatedEntity: handle + bbox + violation
 *         violation.auto_fix: C#이 도면에 적용할 수정 명령
 *
 *   AutoFix type 목록:
 *     ATTRIBUTE   — 블록 속성값 변경   (attribute_tag + new_value)
 *     LAYER       — 레이어 변경        (new_layer)
 *     TEXT_CONTENT— 텍스트 내용 변경   (new_text)
 *     TEXT_HEIGHT — 글자 크기 변경     (new_height)
 *     COLOR       — 색상 변경          (new_color: ACI 인덱스)
 *     LINETYPE    — 선종류 변경        (new_linetype)
 *     LINEWEIGHT  — 선두께 변경        (new_lineweight: mm)
 *     DELETE      — entity 삭제        (추가 파라미터 없음)
 *     MOVE        — 위치 이동          (delta_x, delta_y: 상대 이동량)
 *     ROTATE      — 회전              (angle: 도, base_x, base_y: 기준점)
 *     SCALE       — 크기 조정          (scale_x, scale_y, base_x, base_y)
 *     GEOMETRY    — 기하 좌표 직접 수정 (타입별 파라미터)
 *       LINE    → new_start, new_end
 *       CIRCLE  → new_center, new_radius
 *       ARC     → new_center, new_radius, new_start_angle, new_end_angle
 *       POLYLINE→ new_vertices[]
 */

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CadSllmAgent.Models
{
    /// <summary>JSON new_color: 숫자/문자열( LLM ) 모두 C# int? 로 (역직렬화 실패 → 색 미적용 방지)</summary>
    public sealed class NullableAciIntConverter : JsonConverter<int?>
    {
        public override int? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
        {
            switch (reader.TokenType)
            {
                case JsonTokenType.Null: return null;
                case JsonTokenType.String:
                {
                    var s = reader.GetString();
                    if (string.IsNullOrWhiteSpace(s)) return null;
                    return int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out int v) ? v : null;
                }
                case JsonTokenType.Number:
                    if (reader.TryGetInt32(out int i)) return i;
                    if (reader.TryGetDouble(out double d)) return (int)Math.Round(d);
                    return null;
                default: return null;
            }
        }

        public override void Write(Utf8JsonWriter writer, int? value, JsonSerializerOptions options)
        {
            if (value == null) writer.WriteNullValue();
            else writer.WriteNumberValue(value.Value);
        }
    }

    /// <summary>
    /// Python ChatResponse.annotated_entities 의 각 항목.
    /// EXT-01이 추출한 원본 entity 구조에 violation 필드를 추가한 형태.
    /// handle과 bbox는 원본 값 그대로여야 RevCloud 위치 + DrawingPatcher 수정이 정확하다.
    /// </summary>
    public class AnnotatedEntity
    {
        [JsonPropertyName("handle")]    public string        Handle    { get; set; } = "";
        [JsonPropertyName("type")]      public string        Type      { get; set; } = "";
        [JsonPropertyName("layer")]     public string        Layer     { get; set; } = "";
        [JsonPropertyName("bbox")]      public BoundingBox?  BBox      { get; set; }
        [JsonPropertyName("violation")] public ViolationInfo? Violation { get; set; }
    }

    public class BoundingBox
    {
        [JsonPropertyName("x1")] public double X1 { get; set; }
        [JsonPropertyName("y1")] public double Y1 { get; set; }
        [JsonPropertyName("x2")] public double X2 { get; set; }
        [JsonPropertyName("y2")] public double Y2 { get; set; }
    }

    public class ViolationInfo
    {
        [JsonPropertyName("id")]          public string      Id          { get; set; } = "";
        [JsonPropertyName("type")]        public string?     Type        { get; set; }
        [JsonPropertyName("source")]      public string?     Source      { get; set; }
        [JsonPropertyName("severity")]    public string      Severity    { get; set; } = "Minor"; // Critical|Major|Minor
        [JsonPropertyName("rule")]        public string      Rule        { get; set; } = "";
        [JsonPropertyName("description")] public string      Description { get; set; } = "";
        [JsonPropertyName("suggestion")]  public string      Suggestion  { get; set; } = "";
        /// <summary>1=속성(Safe) 2=블록/심볼(승인) 3=동적블록 4=자유형상(AI_PROPOSAL 덧그리기)</summary>
        [JsonPropertyName("modification_tier")]
        public int ModificationTier { get; set; } = 1;
        /// <summary>Python confidence_score (0.0~1.0). 0.7 미만이면 RevCloud를 AI_REVIEW_LOW 레이어에 점선으로 표시.</summary>
        [JsonPropertyName("confidence_score")]  public double? ConfidenceScore  { get; set; }
        /// <summary>신뢰도가 낮은 이유 (Python confidence_reason)</summary>
        [JsonPropertyName("confidence_reason")] public string? ConfidenceReason { get; set; }
        [JsonPropertyName("auto_fix")]    public AutoFix?    AutoFix     { get; set; }
    }

    /// <summary>
    /// C#이 도면에 직접 적용할 수정 명령.
    /// type 값에 따라 사용하는 필드가 다르다 (주석 참조).
    /// null 필드는 JSON 직렬화 시 생략된다.
    /// </summary>
    public class AutoFix
    {
        /// <summary>수정 타입. …|BLOCK_REPLACE|DYNAMIC_BLOCK_PARAM|…</summary>
        [JsonPropertyName("type")] public string Type { get; set; } = "";
        [JsonPropertyName("modification_tier")]
        public int? ModificationTier { get; set; }

        // ── ATTRIBUTE ────────────────────────────────────────────────
        /// <summary>변경할 블록 속성 태그 (ATTRIBUTE 타입)</summary>
        [JsonPropertyName("attribute_tag")] public string? AttributeTag { get; set; }
        /// <summary>새 속성값 (ATTRIBUTE 타입)</summary>
        [JsonPropertyName("new_value")]     public string? NewValue     { get; set; }

        // ── BLOCK_REPLACE (2단계) ───────────────────────────────────
        [JsonPropertyName("new_block_name")] public string? NewBlockName { get; set; }
        // ── DYNAMIC_BLOCK_PARAM (3단계) ──────────────────────────────
        [JsonPropertyName("param_name")]
        public string? ParamName { get; set; }
        /// <summary>param_name 별칭 (LLM/백엔드 JSON 호환)</summary>
        [JsonPropertyName("parameter_name")]
        public string? ParameterName { get; set; }
        [JsonPropertyName("param_value")]
        public string? ParamValue { get; set; }
        /// <summary>param_value 별칭 (일부 LLM이 value만 보낼 때)</summary>
        [JsonPropertyName("value")]
        public string? ValueParam { get; set; }
        // ── LAYER ────────────────────────────────────────────────────
        /// <summary>변경할 레이어명 (LAYER 타입)</summary>
        [JsonPropertyName("new_layer")]     public string? NewLayer     { get; set; }

        // ── TEXT_CONTENT ─────────────────────────────────────────────
        /// <summary>새 텍스트 내용 (TEXT_CONTENT 타입)</summary>
        [JsonPropertyName("new_text")]      public string? NewText      { get; set; }

        // ── TEXT_HEIGHT ──────────────────────────────────────────────
        /// <summary>새 글자 높이 (TEXT_HEIGHT 타입, 도면 단위)</summary>
        [JsonPropertyName("new_height")]    public double? NewHeight    { get; set; }

        // ── COLOR ─────────────────────────────────────────────────────
        /// <summary>새 색상 ACI 인덱스 (COLOR 타입. 1=빨강, 2=노랑, 3=초록, ...)</summary>
        [JsonPropertyName("new_color")]
        [JsonConverter(typeof(NullableAciIntConverter))]
        public int? NewColor { get; set; }

        // ── LINETYPE ─────────────────────────────────────────────────
        /// <summary>새 선종류 (LINETYPE 타입. "CONTINUOUS", "DASHED", "CENTER" 등)</summary>
        [JsonPropertyName("new_linetype")]  public string? NewLinetype  { get; set; }

        // ── LINEWEIGHT ───────────────────────────────────────────────
        /// <summary>새 선두께 mm (LINEWEIGHT 타입. 0.13, 0.18, 0.25, 0.35, 0.50, 0.70 등)</summary>
        [JsonPropertyName("new_lineweight")] public double? NewLineweight { get; set; }

        // ── MOVE ─────────────────────────────────────────────────────
        /// <summary>X 방향 이동량 (MOVE 타입, 상대 좌표)</summary>
        [JsonPropertyName("delta_x")] public double? DeltaX { get; set; }
        /// <summary>Y 방향 이동량 (MOVE 타입, 상대 좌표)</summary>
        [JsonPropertyName("delta_y")] public double? DeltaY { get; set; }

        // ── ROTATE ───────────────────────────────────────────────────
        /// <summary>회전 각도 도(°), 반시계 양수 (ROTATE 타입)</summary>
        [JsonPropertyName("angle")]  public double? Angle { get; set; }
        /// <summary>회전 기준점 X (ROTATE / SCALE 타입)</summary>
        [JsonPropertyName("base_x")] public double? BaseX { get; set; }
        /// <summary>회전 기준점 Y (ROTATE / SCALE 타입)</summary>
        [JsonPropertyName("base_y")] public double? BaseY { get; set; }

        // ── SCALE ────────────────────────────────────────────────────
        /// <summary>X 방향 배율 (SCALE 타입. 단일 배율이면 scale_x만 지정)</summary>
        [JsonPropertyName("scale_x")] public double? ScaleX { get; set; }
        /// <summary>Y 방향 배율 (SCALE 타입. 생략 시 scale_x와 동일)</summary>
        [JsonPropertyName("scale_y")] public double? ScaleY { get; set; }

        // RECTANGLE_RESIZE / STRETCH_RECT
        /// <summary>움직일 사각형 변: right, left, top, bottom</summary>
        [JsonPropertyName("stretch_side")] public string? StretchSide { get; set; }
        /// <summary>사각형 목표 가로 길이. 닫힌 Polyline에서 반대쪽 변은 고정한다.</summary>
        [JsonPropertyName("new_width")] public double? NewWidth { get; set; }
        // ── GEOMETRY — LINE ───────────────────────────────────────────
        /// <summary>LINE 새 시작점 (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_start")] public FixPoint? NewStart { get; set; }
        /// <summary>LINE 새 끝점 (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_end")]   public FixPoint? NewEnd   { get; set; }

        // ── GEOMETRY — CIRCLE / ARC ───────────────────────────────────
        /// <summary>CIRCLE/ARC 새 중심점 (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_center")] public FixPoint? NewCenter { get; set; }
        /// <summary>CIRCLE/ARC 새 반지름 (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_radius")] public double?   NewRadius { get; set; }
        /// <summary>ARC 새 시작 각도 도(°) (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_start_angle")] public double? NewStartAngle { get; set; }
        /// <summary>ARC 새 끝 각도 도(°) (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_end_angle")]   public double? NewEndAngle   { get; set; }

        // ── GEOMETRY — POLYLINE ───────────────────────────────────────
        /// <summary>POLYLINE 새 꼭짓점 목록 (GEOMETRY 타입)</summary>
        [JsonPropertyName("new_vertices")] public List<FixVertex>? NewVertices { get; set; }
    }

    /// <summary>AutoFix에서 사용하는 좌표 (x, y).</summary>
    public class FixPoint
    {
        [JsonPropertyName("x")] public double X { get; set; }
        [JsonPropertyName("y")] public double Y { get; set; }
    }

    /// <summary>AutoFix GEOMETRY POLYLINE의 꼭짓점. bulge≠0이면 호 세그먼트.</summary>
    public class FixVertex
    {
        [JsonPropertyName("x")]     public double X     { get; set; }
        [JsonPropertyName("y")]     public double Y     { get; set; }
        [JsonPropertyName("bulge")] public double Bulge { get; set; }
    }

    /// <summary>React → C# REVIEW_RESULT 액션의 payload 전체.</summary>
    public class ReviewResult
    {
        [JsonPropertyName("session_id")]         public string             SessionId         { get; set; } = "";
        [JsonPropertyName("reply")]              public string             Reply             { get; set; } = "";
        [JsonPropertyName("annotated_entities")] public List<AnnotatedEntity> AnnotatedEntities { get; set; } = new();
        /// <summary>Python session_meta.domain_type (예: pipe). 배관 검토 시 A-/S- 레이어 2차 필터에 사용.</summary>
        [JsonPropertyName("review_domain")]     public string?            ReviewDomain      { get; set; }
    }
}
