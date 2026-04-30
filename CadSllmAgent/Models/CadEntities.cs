/*
 * File    : CadSllmAgent/Extraction/CadEntities.cs
 * Author  : 김다빈
 * WBS     : EXT-01 (도면 데이터 추출 JSON 변환)
 * Create  : 2026-04-06
 *
 * Description :
 *   AutoCAD 도면에서 추출한 데이터를 담는 C# 모델 클래스 모음.
 *   이 구조가 Python ChatRequest.cad_data 로 그대로 전달되므로
 *   Python 에이전트(RAG·SFT·LLM)가 이 필드명을 그대로 읽는다.
 *
 *   핵심 필드:
 *     CadDrawingData  — 도면 전체 요약 (단위·레이어 목록·entity 목록)
 *     CadEntity       — 단일 객체 (handle, type, layer, bbox + 타입별 추가정보)
 *     CadBBox         — 객체의 도면 좌표 기반 바운딩 박스 (RevCloud 위치 계산에 사용)
 *
 *   Python annotated_entities 반환 시 handle과 bbox를 그대로 돌려줘야
 *   C# RevCloudDrawer가 정확한 위치에 구름 마크를 그릴 수 있다.
 *
 * Modification History :
 *   - 2026-04-15 (김다빈) : elapsed_ms 제거 (LLM 노이즈), drawing_unit 추가 (SFT 거리 판단용)
 *   - 2026-04-25        : DrawingLayout 추가 (Paper Space 레이아웃/뷰포트 모델 좌표 범위)
 *                         CadDrawingData.layouts 필드 추가
 *                         CadEntity.layout_name 필드 추가 (어느 레이아웃 뷰포트에 속하는지)
 */

using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace CadSllmAgent.Models
{
    /// <summary>
    /// EXT-01 추출 결과 최상위 객체.
    /// WebView2 메시지 CAD_DATA_EXTRACTED 의 payload로 전송된다.
    /// Python ChatRequest.cad_data 에 그대로 들어간다.
    /// </summary>
    public class CadDrawingData
    {
        /// <summary>
        /// 도면 좌표 단위. LLM·SFT가 치수(measurement, length 등)를 건축법 기준과 비교할 때 필수.
        /// AutoCAD db.Insunits 기반: "mm" | "cm" | "m" | "inch" | "feet" | "unknown"
        /// </summary>
        [JsonPropertyName("drawing_unit")]  public string              DrawingUnit { get; set; } = "unknown";
        [JsonPropertyName("layer_count")]   public int                 LayerCount  { get; set; }
        [JsonPropertyName("entity_count")]  public int                 EntityCount { get; set; }
        [JsonPropertyName("layers")]        public List<CadLayer>      Layers      { get; set; } = new();
        [JsonPropertyName("entities")]      public List<CadEntity>     Entities    { get; set; } = new();
        /// <summary>
        /// Paper Space 레이아웃별 뷰포트가 참조하는 모델 좌표 범위 목록.
        /// 하나의 DWG에 여러 도면이 배치된 경우 각 도면 영역을 구분하는 데 사용.
        /// 빈 배열이면 Model Space 전수 추출이거나 Paper Space 없음.
        /// </summary>
        [JsonPropertyName("layouts")]       public List<DrawingLayout> Layouts     { get; set; } = new();
    }

    /// <summary>
    /// Paper Space 레이아웃의 뷰포트가 가리키는 모델 좌표 범위.
    /// CadDataExtractor.ExtractLayoutRegions()에서 추출.
    /// 하나의 Layout에 여러 Viewport가 있으면 Viewport마다 항목 1개씩 생성.
    /// </summary>
    public class DrawingLayout
    {
        [JsonPropertyName("name")]    public string Name { get; set; } = "";
        [JsonPropertyName("x1")]     public double X1   { get; set; }
        [JsonPropertyName("y1")]     public double Y1   { get; set; }
        [JsonPropertyName("x2")]     public double X2   { get; set; }
        [JsonPropertyName("y2")]     public double Y2   { get; set; }

        public bool Contains(double cx, double cy) =>
            cx >= X1 && cx <= X2 && cy >= Y1 && cy <= Y2;
    }

    /// <summary>EXT-02: 레이어 메타데이터 (색상, 잠금 포함)</summary>
    public class CadLayer
    {
        [JsonPropertyName("name")]         public string  Name        { get; set; } = "";
        [JsonPropertyName("entity_count")] public int     EntityCount { get; set; }
        [JsonPropertyName("is_on")]        public bool    IsOn        { get; set; }
        // EXT-02 추가 필드
        [JsonPropertyName("is_locked")]    public bool    IsLocked    { get; set; }
        [JsonPropertyName("color")]        public string? Color       { get; set; } // "BYLAYER" 또는 색상 인덱스
        [JsonPropertyName("linetype")]     public string? Linetype    { get; set; }
        [JsonPropertyName("lineweight")]   public string? Lineweight  { get; set; }
        [JsonPropertyName("is_plottable")] public bool    IsPlottable { get; set; }
    }

    /// <summary>
    /// 단일 AutoCAD entity.
    /// handle + bbox 는 Python이 annotated_entities 반환 시 그대로 사용한다.
    /// </summary>
    public class CadEntity
    {
        // ── 공통 ─────────────────────────────────────────────────────
        [JsonPropertyName("handle")]      public string                      Handle      { get; set; } = "";
        [JsonPropertyName("type")]        public string                      Type        { get; set; } = "";
        [JsonPropertyName("layer")]       public string                      Layer       { get; set; } = "";
        [JsonPropertyName("bbox")]        public CadBBox?                    BBox        { get; set; }
        [JsonPropertyName("color")]       public string?                     Color       { get; set; }
        [JsonPropertyName("linetype")]    public string?                     Linetype    { get; set; }
        [JsonPropertyName("lineweight")]  public string?                     Lineweight  { get; set; }
        /// <summary>
        /// 이 entity가 속하는 Paper Space 레이아웃 이름.
        /// null이면 어느 뷰포트 범위에도 해당 없음(또는 레이아웃 없는 DWG).
        /// 하나의 DWG에 여러 도면이 배치된 경우 도면 구분에 사용.
        /// </summary>
        [JsonPropertyName("layout_name")] public string?                    LayoutName  { get; set; }

        // ── LINE ─────────────────────────────────────────────────────
        [JsonPropertyName("start")]       public CadPoint?                   Start       { get; set; }
        [JsonPropertyName("end")]         public CadPoint?                   End         { get; set; }
        [JsonPropertyName("length")]      public double?                     Length      { get; set; }
        [JsonPropertyName("angle")]       public double?                     Angle       { get; set; }

        // ── CIRCLE / ARC / ELLIPSE 공통 ──────────────────────────────
        [JsonPropertyName("center")]      public CadPoint?                   Center      { get; set; }
        [JsonPropertyName("radius")]      public double?                     Radius      { get; set; }
        [JsonPropertyName("diameter")]    public double?                     Diameter    { get; set; }
        [JsonPropertyName("start_angle")] public double?                     StartAngle  { get; set; }
        [JsonPropertyName("end_angle")]   public double?                     EndAngle    { get; set; }
        [JsonPropertyName("arc_length")]  public double?                     ArcLength   { get; set; }

        // ── ELLIPSE 전용 ─────────────────────────────────────────────
        [JsonPropertyName("major_axis")]  public CadPoint?                   MajorAxis   { get; set; }
        [JsonPropertyName("minor_ratio")] public double?                     MinorRatio  { get; set; }

        // ── POLYLINE / SPLINE ────────────────────────────────────────
        [JsonPropertyName("vertices")]    public List<CadVertex>?            Vertices    { get; set; }
        [JsonPropertyName("fit_points")]  public List<CadPoint>?             FitPoints   { get; set; }
        [JsonPropertyName("is_closed")]   public bool?                       IsClosed    { get; set; }
        [JsonPropertyName("perimeter")]   public double?                     Perimeter   { get; set; }
        [JsonPropertyName("area")]        public double?                     Area        { get; set; }

        // ── BLOCK ────────────────────────────────────────────────────
        [JsonPropertyName("block_name")]  public string?                     BlockName   { get; set; }
        [JsonPropertyName("insert_point")]public CadPoint?                   InsertPoint { get; set; }
        [JsonPropertyName("rotation")]    public double?                     Rotation    { get; set; }
        [JsonPropertyName("scale_x")]     public double?                     ScaleX      { get; set; }
        [JsonPropertyName("scale_y")]     public double?                     ScaleY      { get; set; }
        [JsonPropertyName("attributes")]  public Dictionary<string, string>? Attributes  { get; set; }

        // ── MTEXT / TEXT ─────────────────────────────────────────────
        [JsonPropertyName("text")]             public string?   Text            { get; set; }
        [JsonPropertyName("text_height")]      public double?   TextHeight      { get; set; }
        [JsonPropertyName("text_width")]       public double?   TextWidth       { get; set; }
        /// <summary>MTEXT 정렬 기준점 (TOP_LEFT / MIDDLE_CENTER 등)</summary>
        [JsonPropertyName("attachment_point")] public string?   AttachmentPoint { get; set; }

        // ── DIMENSION ────────────────────────────────────────────────
        [JsonPropertyName("measurement")]      public double?   Measurement     { get; set; }
        [JsonPropertyName("dim_text")]         public string?   DimText         { get; set; }
        [JsonPropertyName("dim_style")]        public string?   DimStyle        { get; set; }
        [JsonPropertyName("dim_type")]         public string?   DimType         { get; set; }
        /// <summary>치수 첫 번째 보조선 기준점 — 어느 엔티티를 재는지 특정에 필수</summary>
        [JsonPropertyName("xline1_point")]     public CadPoint? Xline1Point     { get; set; }
        /// <summary>치수 두 번째 보조선 기준점</summary>
        [JsonPropertyName("xline2_point")]     public CadPoint? Xline2Point     { get; set; }
        /// <summary>치수선(dim line)이 지나는 기준점</summary>
        [JsonPropertyName("dim_line_point")]   public CadPoint? DimLinePoint    { get; set; }
        /// <summary>치수 텍스트 위치</summary>
        [JsonPropertyName("text_position")]    public CadPoint? TextPosition    { get; set; }

        // ── POLYLINE (추가) ──────────────────────────────────────────
        /// <summary>폴리라인 균일 두께 — 벽체를 두꺼운 폴리라인으로 표현할 때 벽 두께 정보</summary>
        [JsonPropertyName("constant_width")]   public double?   ConstantWidth   { get; set; }

        // ── HATCH (신규) ─────────────────────────────────────────────
        /// <summary>해칭 패턴명 (SOLID / ANSI31 / AR-BRTIL 등). 재료·구획 구분에 사용.</summary>
        [JsonPropertyName("pattern_name")]     public string?   PatternName     { get; set; }
        [JsonPropertyName("pattern_scale")]    public double?   PatternScale    { get; set; }
        [JsonPropertyName("pattern_angle")]    public double?   PatternAngle    { get; set; }
        /// <summary>해칭 영역 면적 — 실내 면적·방화구획 면적 산정에 사용</summary>
        [JsonPropertyName("hatch_area")]       public double?   HatchArea       { get; set; }
        /// <summary>해칭 경계 루프 수 (1=단순 폐합, 2+=도넛형)</summary>
        [JsonPropertyName("boundary_count")]   public int?      BoundaryCount   { get; set; }

        // ── SOLID (신규) ─────────────────────────────────────────────
        /// <summary>2D Solid 네 꼭짓점. 채워진 삼각형/사각형 표현에 사용.</summary>
        [JsonPropertyName("corner1")]          public CadPoint? Corner1         { get; set; }
        [JsonPropertyName("corner2")]          public CadPoint? Corner2         { get; set; }
        [JsonPropertyName("corner3")]          public CadPoint? Corner3         { get; set; }
        [JsonPropertyName("corner4")]          public CadPoint? Corner4         { get; set; }

        // ── BLOCK (추가) ─────────────────────────────────────────────
        /// <summary>Z 방향 스케일 — 높이 정보 포함 블록에 사용</summary>
        [JsonPropertyName("scale_z")]          public double?   ScaleZ          { get; set; }
    }

    public class CadBBox
    {
        [JsonPropertyName("x1")] public double X1 { get; set; }
        [JsonPropertyName("y1")] public double Y1 { get; set; }
        [JsonPropertyName("x2")] public double X2 { get; set; }
        [JsonPropertyName("y2")] public double Y2 { get; set; }
    }

    /// <summary>도면 좌표 (x, y). CadDataExtractor의 기하 추출에 사용.</summary>
    public class CadPoint
    {
        [JsonPropertyName("x")] public double X { get; set; }
        [JsonPropertyName("y")] public double Y { get; set; }
        [JsonPropertyName("z")] public double Z { get; set; }
    }

    /// <summary>폴리라인 꼭짓점 (x, y, bulge).</summary>
    public class CadVertex
    {
        [JsonPropertyName("x")]     public double X     { get; set; }
        [JsonPropertyName("y")]     public double Y     { get; set; }
        [JsonPropertyName("bulge")] public double Bulge { get; set; }
    }
}
