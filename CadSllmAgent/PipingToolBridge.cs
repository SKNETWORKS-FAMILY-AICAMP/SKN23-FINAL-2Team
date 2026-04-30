/*
 * File    : CadSllmAgent/PipingToolBridge.cs
 * Description : `ApiClient.SendCadDataAsync` 가 payload.piping_tool / domain_type / dwg_compare_mode 로
 *               서버와 실제로 연동한다(추가 필드, 기존 동작과 동일). LLM tool 이름은 Python PIPING_SUB_AGENT_TOOLS.
 *
 * | python tool            | C# |
 * |------------------------|----|
 * | call_review_agent      | ApiClient + CadDataExtractor → /api/v1/cad/analyze |
 * | (나머지)               | 서버 전용 또는 Socket(RevCloudDrawer) |
 */
namespace CadSllmAgent
{
    public static class PipingToolNames
    {
        public const string CallQueryAgent = "call_query_agent";
        public const string CallReviewAgent = "call_review_agent";
        public const string CallActionAgent = "call_action_agent";
        public const string GetCadEntityInfo = "get_cad_entity_info";
    }

    /// <summary>문서/주석용: C# 소스 ↔ 배관 tool 역할</summary>
    public static class PipingToolBridge
    {
        public const string CSharpExtractor = "Extraction.CadDataExtractor";
        public const string CSharpRevCloud = "Review.RevCloudDrawer";
        public const string CSharpApiClient = "Services.ApiClient";
    }
}
