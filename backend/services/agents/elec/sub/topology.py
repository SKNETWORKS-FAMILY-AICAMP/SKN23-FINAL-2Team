"""
File    : backend/services/agents/elec/sub/topology.py
Author  : 김지우
Create  : 2026-04-24
Description : CAD LINE/WIRE 끝점 근접으로 전기 회로 경로(circuit_run)를 구성하고
              연결된 분전반·기기를 식별합니다.
              배관 PipeTopologyBuilder 패턴을 전기 도메인에 적용.

출력 스키마:
  circuit_runs: [
    {
      run_id       : int,
      handles      : [str, ...],           # 구성 LINE handle
      total_length : float,
      voltage      : float,                # 회로 전압 (V)
      cable_sqmm   : float,                # 전선 굵기 (SQ)
      connected_panels: [str, ...]         # 연결된 분전반 handle
    }, ...
  ]
  panel_graph : {panel_handle: [연결된 device_handle, ...]}
  summary     : {run_count, total_lines, unconnected_wires, panel_count}
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any

_log = logging.getLogger(__name__)

_CONN_TOL = 50.0           # 두 끝점이 이 거리 이내면 연결로 판정 (mm)
_BREAK_DETECT_MAX = 200.0  # 이 거리 이내의 미연결 끝점 쌍 → 단선 후보로 보고 (mm)
                            # 2000mm는 너무 넓어 정상 회로 단말부도 오보됨 → 200mm로 축소
_BLOCK_CONN_TOL_MUL = 1.5
_MAX_LINES = 5000

_LINE_RAW  = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_PANEL_PREFIXES = ("PNL", "MCC", "CB", "MCB", "MCCB", "SW", "TR")


def _pt(d: dict, *keys) -> tuple[float, float] | None:
    for k in keys:
        p = d.get(k)
        if isinstance(p, dict) and "x" in p:
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError):
                pass
    return None


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _endpoints(e: dict) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    rt = str(e.get("raw_type") or "").upper()
    if rt in ("POLYLINE", "LWPOLYLINE"):
        pts = e.get("points") or []
        if len(pts) >= 2:
            try:
                s = (float(pts[0]["x"]), float(pts[0]["y"]))
                t = (float(pts[-1]["x"]), float(pts[-1]["y"]))
                return s, t
            except (KeyError, TypeError, ValueError):
                pass
    s = _pt(e, "start_point", "start", "position", "center")
    t = _pt(e, "end_point", "end")
    return s, t


class ElecTopologyBuilder:
    """전기 도면 회로 경로 및 분전반 연결 그래프 구성."""

    def build(self, elements: list[dict]) -> dict:
        def _etype(e: dict) -> str:
            return str(e.get("raw_type") or e.get("type") or "").upper()

        lines  = [e for e in elements if _etype(e) in _LINE_RAW]
        blocks = [e for e in elements if _etype(e) in _BLOCK_RAW]

        if len(lines) > _MAX_LINES:
            _log.warning("[ElecTopology] LINE %d개 초과 — 앞 %d개만 처리", len(lines), _MAX_LINES)
            lines = lines[:_MAX_LINES]

        # Union-Find로 연결 그룹 구성
        parent = list(range(len(lines)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            pi, pj = find(i), find(j)
            if pi != pj:
                parent[pi] = pj

        ep_cache: dict[int, tuple] = {}
        for i, line in enumerate(lines):
            ep_cache[i] = _endpoints(line)

        for i in range(len(lines)):
            si, ti = ep_cache[i]
            if not si and not ti:
                continue
            for j in range(i + 1, len(lines)):
                sj, tj = ep_cache[j]
                pts_i = [p for p in (si, ti) if p]
                pts_j = [p for p in (sj, tj) if p]
                for pi in pts_i:
                    for pj in pts_j:
                        if _dist(pi, pj) <= _CONN_TOL:
                            union(i, j)

        # 그룹별 circuit_run 구성
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(len(lines)):
            groups[find(i)].append(i)

        circuit_runs = []
        for run_id, (_, idxs) in enumerate(groups.items()):
            group_lines = [lines[i] for i in idxs]
            handles = [str(e.get("handle") or "") for e in group_lines]
            total_length = sum(float(e.get("length") or 0) for e in group_lines)
            voltages = [float(e.get("voltage") or 0) for e in group_lines if e.get("voltage")]
            sqmms = [float(e.get("cable_sqmm") or 0) for e in group_lines if e.get("cable_sqmm")]
            circuit_runs.append({
                "run_id": run_id,
                "handles": handles,
                "total_length": round(total_length, 2),
                "voltage": voltages[0] if voltages else 0.0,
                "cable_sqmm": sqmms[0] if sqmms else 0.0,
                "connected_panels": [],
            })

        # 분전반-회로 연결
        panel_graph: dict[str, list[str]] = {}
        for block in blocks:
            bh = str(block.get("handle") or "")
            bn = str(block.get("block_name") or block.get("standard_name") or "").upper()
            is_panel = any(bn.startswith(p) for p in _PANEL_PREFIXES)
            if not is_panel:
                continue
            bc = _pt(block, "center", "position", "insert_point")
            if not bc:
                continue
            panel_graph[bh] = []
            tol = _CONN_TOL * _BLOCK_CONN_TOL_MUL
            for run in circuit_runs:
                for hi in run["handles"]:
                    line_e = next((e for e in lines if str(e.get("handle") or "") == hi), None)
                    if not line_e:
                        continue
                    for ep in filter(None, _endpoints(line_e)):
                        if _dist(bc, ep) <= tol:
                            run["connected_panels"].append(bh)
                            panel_graph[bh].append(hi)
                            break

        unconnected = sum(1 for r in circuit_runs if not r["connected_panels"])

        # ── [노이즈 필터] 짧은 고립 선 핸들 사전 수집 ──────────────────────────
        # 단독 LINE 1개짜리 run이 100mm 미만이면 도면 노이즈(치수선 보조, 해칭 등)
        # → dangling/broken 분석에서 제외하여 환각 방지
        _NOISE_LINE_MAX_MM = 100.0
        noise_handles: set[str] = set()
        for run in circuit_runs:
            if len(run["handles"]) == 1 and run["total_length"] < _NOISE_LINE_MAX_MM:
                noise_handles.add(run["handles"][0])
        if noise_handles:
            _log.debug("[ElecTopology] 노이즈 고립 선 %d개 제외 (dangling 분석)", len(noise_handles))

        # ── 단선(끊어진 선) 감지 ────────────────────────────────────────────────
        # 각 LINE 끝점 중 어떤 다른 끝점과도 _CONN_TOL 이내에 없는 것이 "dangling endpoint"
        # dangling endpoint 쌍 중 _BREAK_DETECT_MAX 이내면 → 의도적 단선으로 보고
        all_endpoints: list[dict] = []  # {handle, side, x, y}
        for line_ent in lines:
            h = str(line_ent.get("handle") or "")
            if h in noise_handles:
                continue  # 노이즈 선은 분석에서 제외
            s, t = _endpoints(line_ent)
            if s:
                all_endpoints.append({"handle": h, "side": "start", "x": s[0], "y": s[1]})
            if t:
                all_endpoints.append({"handle": h, "side": "end",   "x": t[0], "y": t[1]})

        # 연결 여부 판정 — O(N²) 이지만 일반 도면 수준(수천 개)에서 충분히 빠름
        dangling: list[dict] = []
        for i, ep in enumerate(all_endpoints):
            connected = False
            for j, other in enumerate(all_endpoints):
                if i == j or ep["handle"] == other["handle"]:
                    continue
                if math.hypot(ep["x"] - other["x"], ep["y"] - other["y"]) <= _CONN_TOL:
                    connected = True
                    break
            if not connected:
                dangling.append(ep)

        # ── [핵심 필터] 블록(INSERT) 근처의 dangling endpoint 제거 ────────────
        # 전선 끝점이 블록 삽입점(모터, 차단기, 분전반 등) 근처에서 끝나면
        # 이는 "블록에 연결됨"을 의미하지, 단선이 아님.
        # 블록 크기를 고려하여 _CONN_TOL * _BLOCK_CONN_TOL_MUL 범위 내 제거.
        block_conn_radius = _CONN_TOL * _BLOCK_CONN_TOL_MUL
        block_points: list[tuple[float, float]] = []
        for e in blocks:
            pt = _pt(e, "insert_point", "position", "center")
            if pt:
                block_points.append(pt)

        if block_points:
            pre_filter_count = len(dangling)
            dangling = [
                ep for ep in dangling
                if not any(
                    math.hypot(ep["x"] - bp[0], ep["y"] - bp[1]) <= block_conn_radius
                    for bp in block_points
                )
            ]
            block_filtered = pre_filter_count - len(dangling)
            if block_filtered:
                _log.debug(
                    "[ElecTopology] 블록 근처 dangling %d개 제거 (블록 연결로 판정)",
                    block_filtered,
                )

        # dangling 쌍 중 가까운 것들 → 단선(broken segment) 후보
        broken_segments: list[dict] = []
        reported: set[frozenset] = set()
        for i, a in enumerate(dangling):
            for b in dangling[i + 1:]:
                if a["handle"] == b["handle"]:
                    continue  # 같은 선의 두 끝점 — 단독 선분, 단선 아님
                gap_mm = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                if gap_mm <= _BREAK_DETECT_MAX:
                    pair_key = frozenset({a["handle"], b["handle"]})
                    if pair_key in reported:
                        continue
                    reported.add(pair_key)
                    broken_segments.append({
                        "handle_a":  a["handle"],
                        "side_a":    a["side"],
                        "handle_b":  b["handle"],
                        "side_b":    b["side"],
                        "gap_mm":    round(gap_mm, 2),
                        "midpoint":  {
                            "x": round((a["x"] + b["x"]) / 2, 4),
                            "y": round((a["y"] + b["y"]) / 2, 4),
                        },
                    })

        if broken_segments:
            _log.warning(
                "[ElecTopology] 단선 감지: %d건 (dangling=%d)", len(broken_segments), len(dangling)
            )

        # ── 노이즈 회로 제거 (circuit_runs에서도) ────────────────────────────────
        # 단독 LINE 1개짜리 run이 100mm 미만이면 도면 노이즈
        # → compliance AI에 전달 시 "0.0SQ 위반" 환각의 원인이 되므로 제거
        clean_runs = [
            r for r in circuit_runs
            if not (len(r["handles"]) == 1 and r["total_length"] < _NOISE_LINE_MAX_MM)
        ]
        noise_removed = len(circuit_runs) - len(clean_runs)
        if noise_removed:
            _log.debug("[ElecTopology] 노이즈 회로 %d개 제거 (circuit_runs)", noise_removed)

        unconnected = sum(1 for r in clean_runs if not r["connected_panels"])

        _log.info(
            "[ElecTopology] circuit_runs=%d (noise_removed=%d) unconnected=%d panels=%d broken=%d",
            len(clean_runs), noise_removed, unconnected, len(panel_graph), len(broken_segments),
        )
        return {
            "circuit_runs":    clean_runs,
            "panel_graph":     panel_graph,
            "broken_segments": broken_segments,
            "dangling_endpoints": dangling,
            "summary": {
                "run_count":         len(clean_runs),
                "total_lines":       len(lines),
                "unconnected_wires": unconnected,
                "panel_count":       len(panel_graph),
                "broken_count":      len(broken_segments),
                "noise_removed":     noise_removed,
            },
        }
