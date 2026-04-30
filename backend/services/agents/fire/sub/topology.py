"""
File    : backend/services/agents/fire/sub/topology.py
Author  : 김민정
Create  : 2026-04-24
Description : CAD LINE 끝점 근접으로 소방 배관 경로(pipe_run)를 구성하고
              스프링클러 헤드·감지기 연결 그래프와 커버리지 존을 구성합니다.
              배관 PipeTopologyBuilder 패턴을 소방 도메인에 적용.

출력 스키마:
  pipe_runs: [
    {
      run_id       : int,
      handles      : [str, ...],
      total_length : float,
      connected_heads: [str, ...]          # 연결된 헤드/감지기 handle
    }, ...
  ]
  coverage_zones: [
    {
      zone_id         : int,
      head_handles    : [str, ...],
      estimated_area_sqm: float
    }, ...
  ]
  head_graph : {head_handle: [연결된 pipe_handle, ...]}
  summary    : {run_count, head_count, coverage_area_sqm}
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any

_log = logging.getLogger(__name__)

_CONN_TOL = 100.0          # 소방 헤드 연결 허용 오차 (배관보다 여유)
_HEAD_TOL_MUL = 4
_MAX_LINES = 2000

_LINE_RAW  = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE"})
_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_HEAD_PREFIXES = ("SPK", "HYD", "FDH", "SMK", "HTD")

# 스프링클러 헤드 표준 커버 반경 (NFSC 103 기준, mm)
_SPK_COVER_RADIUS_MM = 2300.0


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
                return (float(pts[0]["x"]), float(pts[0]["y"])), (float(pts[-1]["x"]), float(pts[-1]["y"]))
            except (KeyError, TypeError, ValueError):
                pass
    return _pt(e, "start_point", "start", "position"), _pt(e, "end_point", "end")


class FireTopologyBuilder:
    """소방 도면 배관 경로 및 헤드 연결 그래프 구성."""

    def build(self, elements: list[dict]) -> dict:
        lines  = [e for e in elements if str(e.get("raw_type") or "").upper() in _LINE_RAW]
        blocks = [e for e in elements if str(e.get("raw_type") or "").upper() in _BLOCK_RAW]

        if len(lines) > _MAX_LINES:
            _log.warning("[FireTopology] LINE %d개 초과 — 앞 %d개만 처리", len(lines), _MAX_LINES)
            lines = lines[:_MAX_LINES]

        # Union-Find
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

        ep_cache = {i: _endpoints(lines[i]) for i in range(len(lines))}
        for i in range(len(lines)):
            si, ti = ep_cache[i]
            for j in range(i + 1, len(lines)):
                sj, tj = ep_cache[j]
                for pi in filter(None, (si, ti)):
                    for pj in filter(None, (sj, tj)):
                        if _dist(pi, pj) <= _CONN_TOL:
                            union(i, j)

        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(len(lines)):
            groups[find(i)].append(i)

        pipe_runs = []
        for run_id, (_, idxs) in enumerate(groups.items()):
            group_lines = [lines[i] for i in idxs]
            handles = [str(e.get("handle") or "") for e in group_lines]
            total_length = sum(float(e.get("length") or 0) for e in group_lines)
            pipe_runs.append({
                "run_id": run_id,
                "handles": handles,
                "total_length": round(total_length, 2),
                "connected_heads": [],
            })

        # 헤드/감지기 연결
        head_graph: dict[str, list[str]] = {}
        head_positions: list[tuple[str, tuple[float, float]]] = []
        tol = _CONN_TOL * _HEAD_TOL_MUL
        for block in blocks:
            bh = str(block.get("handle") or "")
            bn = str(block.get("block_name") or block.get("standard_name") or "").upper()
            if not any(bn.startswith(p) for p in _HEAD_PREFIXES):
                continue
            bc = _pt(block, "center", "position", "insert_point")
            if not bc:
                continue
            head_positions.append((bh, bc))
            head_graph[bh] = []
            for run in pipe_runs:
                for hi in run["handles"]:
                    le = next((e for e in lines if str(e.get("handle") or "") == hi), None)
                    if not le:
                        continue
                    for ep in filter(None, _endpoints(le)):
                        if _dist(bc, ep) <= tol:
                            run["connected_heads"].append(bh)
                            head_graph[bh].append(hi)
                            break

        # 커버리지 존 (단순 그룹: 반경 내 헤드 클러스터링)
        coverage_zones = self._build_coverage_zones(head_positions)

        total_coverage = sum(z["estimated_area_sqm"] for z in coverage_zones)
        _log.info(
            "[FireTopology] pipe_runs=%d heads=%d coverage_zones=%d total_area=%.1f sqm",
            len(pipe_runs), len(head_positions), len(coverage_zones), total_coverage,
        )
        return {
            "pipe_runs": pipe_runs,
            "coverage_zones": coverage_zones,
            "head_graph": head_graph,
            "summary": {
                "run_count": len(pipe_runs),
                "head_count": len(head_positions),
                "coverage_area_sqm": round(total_coverage, 2),
            },
        }

    def _build_coverage_zones(
        self,
        head_positions: list[tuple[str, tuple[float, float]]],
    ) -> list[dict]:
        """스프링클러 헤드를 반경 기반으로 그룹화하여 커버리지 존 구성."""
        if not head_positions:
            return []
        used = [False] * len(head_positions)
        zones = []
        zone_id = 0
        for i, (bh_i, pt_i) in enumerate(head_positions):
            if used[i]:
                continue
            group = [bh_i]
            used[i] = True
            for j, (bh_j, pt_j) in enumerate(head_positions):
                if used[j]:
                    continue
                if _dist(pt_i, pt_j) <= _SPK_COVER_RADIUS_MM * 2:
                    group.append(bh_j)
                    used[j] = True
            area_sqm = len(group) * math.pi * (_SPK_COVER_RADIUS_MM / 1000) ** 2
            zones.append({
                "zone_id": zone_id,
                "head_handles": group,
                "estimated_area_sqm": round(area_sqm, 2),
            })
            zone_id += 1
        return zones
