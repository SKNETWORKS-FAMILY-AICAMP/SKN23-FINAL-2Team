"""
File    : mcp_server/server.py
Author  : 양창일
WBS     : AI-02, INF-06
Create  : 2026-04-03
Description :
    FastMCP 기반 MCP 서버 진입점.
    AutoCAD 플러그인(C#)이 MCP 프로토콜로 직접 호출하는 툴 서버.

    등록된 툴:
        - extract_cad_data    : CAD JSON 파싱 (EXT-01, 양창일)
        - search_regulation   : 법규 RAG 검색 (DB-02/03, 송주엽)
        - lookup_regulation   : 규정 코드 단건 조회 (DB-02, 송주엽)
        - free_query          : 자유질의 (AI-03 MCP 대체, 양창일)

    실행:
        python -m mcp_server.server          # stdio 모드 (MCP 표준)
        fastmcp dev mcp_server/server.py     # 개발 모드 (MCP Inspector)

TODO(양창일):
    - FastMCP 설치 후 import 활성화 (uv pip install fastmcp)
    - AutoCAD 플러그인 MCP 연결 테스트 (INF-06)
    - 자유질의 툴에 AI-03 재질문 로직 연동
"""

from fastmcp import FastMCP
from mcp_server.tools.cad_tools import extract_cad_data
from mcp_server.tools.rag_tools import search_regulation
from mcp_server.tools.regulation_tools import lookup_regulation
from mcp_server.tools.free_query import free_query

mcp = FastMCP(
    name="CAD-SLLM MCP Server",
    instructions="AutoCAD 도면 검토를 위한 MCP 서버. 법규 검색, 도면 파싱, 자유질의 툴을 제공합니다.",
)

# 툴 등록
mcp.tool()(extract_cad_data)
mcp.tool()(search_regulation)
mcp.tool()(lookup_regulation)
mcp.tool()(free_query)

if __name__ == "__main__":
    mcp.run()   # stdio 모드로 실행 (MCP 표준 프로토콜)
