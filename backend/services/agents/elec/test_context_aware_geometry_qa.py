from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.agents.elec.sub.deterministic_checker import run_deterministic_checks
from backend.services.agents.elec.sub.review.revision import RevisionAgent
from backend.services.agents.elec.sub.semantic_utils import build_table_mapping_debug
from backend.services.agents.elec.sub.topology import ElecTopologyBuilder


def _circle(handle: str, x: float, y: float, radius: float = 9.0, layer: str = "E-TERM") -> dict:
    return {
        "handle": handle,
        "type": "CIRCLE",
        "layer": layer,
        "center": {"x": x, "y": y},
        "radius": radius,
    }


def _text(handle: str, text: str, x: float, y: float, layer: str = "E-TEXT") -> dict:
    return {
        "handle": handle,
        "type": "TEXT",
        "layer": layer,
        "text": text,
        "insert_point": {"x": x, "y": y},
    }


def _line(handle: str, sx: float, sy: float, ex: float, ey: float, layer: str = "E-WIRE") -> dict:
    return {
        "handle": handle,
        "type": "LINE",
        "layer": layer,
        "start": {"x": sx, "y": sy},
        "end": {"x": ex, "y": ey},
        "length": ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5,
    }


def _arc(handle: str, x1: float, y1: float, x2: float, y2: float, layer: str = "E-WIRE") -> dict:
    return {
        "handle": handle,
        "type": "ARC",
        "layer": layer,
        "linetype": "ByLayer",
        "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        "start": {"x": x1, "y": y1},
        "end": {"x": x2, "y": y2},
    }


def _topology(circles: list[dict], drawing_intent: str = "DETAIL_DRAWING") -> dict:
    return {
        "terminal_candidates": [
            {
                "circles": [
                    {
                        "handle": c["handle"],
                        "member_handles": [c["handle"]],
                        "center": c["center"],
                        "radius": c["radius"],
                    }
                    for c in circles
                ],
                "bbox": {"x1": -10, "y1": -10, "x2": 120, "y2": 120},
            }
        ],
        "broken_segments": [],
        "summary": {"drawing_intent": drawing_intent},
    }


def test_detached_circle_move_fix() -> None:
    circles = [
        _circle("A", 0, 0),
        _circle("B", 40, 0),
        _circle("C", 0, 35),
        _circle("D", 40, 95),
    ]
    topology = _topology(circles)
    violations = run_deterministic_checks(circles, {}, topology)
    detached = [v for v in violations if v["violation_type"] == "terminal_detached_circle"]
    assert detached, violations
    assert detached[0].get("expected_center"), detached[0]

    fixes = RevisionAgent().calculate_fix(detached, {})
    assert fixes[0]["proposed_fix"]["action"] == "move_entity", fixes
    assert fixes[0]["proposed_fix"].get("auto_fix", {}).get("type") == "MOVE", fixes


def test_e1_or_test_circle_is_suppressed() -> None:
    base = [_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)]
    orphan = _circle("E1C", 180, 0)
    elements = base + [orphan, _text("T1", "E1", 181, 2)]
    topology = _topology(base)
    violations = run_deterministic_checks(elements, {}, topology)
    assert not [v for v in violations if v["violation_type"] == "terminal_orphan_circle"], violations
    suppressed = topology.get("terminal_debug", {}).get("suppressed_orphan_candidates", [])
    assert any(item["handle"] == "E1C" for item in suppressed), topology.get("terminal_debug")


def test_repeated_test_symbols_do_not_raise_geometry_violation() -> None:
    base = [_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)]
    elements = base + [
        _circle("TP1", 170, 0, layer="E-TEST"),
        _text("TXT1", "TEST", 171, 2),
        _circle("TP2", 210, 0, layer="E-TEST"),
        _text("TXT2", "TEST", 211, 2),
    ]
    topology = _topology(base)
    violations = run_deterministic_checks(elements, {}, topology)
    assert not [v for v in violations if v.get("category") == "geometry_qa"], violations
    assert len(topology.get("terminal_debug", {}).get("suppressed_orphan_candidates", [])) >= 2


def test_e1_e2_grounding_node_cluster_is_not_terminal_deformation() -> None:
    circles = [
        _circle("E1A", 0, 0, layer="E-GROUND"),
        _circle("E1B", 0, 60, layer="E-GROUND"),
        _circle("E1C", 0, 120, layer="E-GROUND"),
        _circle("E2A", 300, 0, layer="E-GROUND"),
    ]
    topology = _topology(circles, drawing_intent="GROUNDING_PLAN")
    topology["terminal_candidates"][0]["nearby_texts"] = [
        {"text": "E1", "handle": "TXT_E1"},
        {"text": "E2", "handle": "TXT_E2"},
    ]
    violations = run_deterministic_checks(circles + [_text("TXT_E1", "E1", 5, 0), _text("TXT_E2", "E2", 305, 0)], {}, topology)
    assert not [v for v in violations if v.get("category") == "geometry_qa"], violations
    assert topology.get("terminal_debug", {}).get("suppressed_orphan_candidates"), topology.get("terminal_debug")


def test_detail_drawing_suppresses_circuit_violations() -> None:
    topology = _topology([_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)])
    topology["broken_segments"] = [{"handle_a": "L1", "handle_b": "L2", "gap_mm": 5}]
    violations = run_deterministic_checks([], {}, topology)
    assert not [v for v in violations if v["violation_type"] == "open_circuit_error"], violations


def test_debug_fields_are_written_to_topology() -> None:
    circles = [_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)]
    topology = _topology(circles)
    run_deterministic_checks(circles, {}, topology)
    assert "terminal_debug" in topology, topology
    assert "drawing_internal_standards" in topology, topology


def test_grounding_plan_intent_and_disconnected_ground_line() -> None:
    elements = [
        _line("G1", 0, 0, 400, 0, "E-GROUND"),
        _line("G2", 520, 0, 920, 0, "E-GROUND"),
        _text("GT", "22.9kV GROUND TEST E1 E2 CABLE TRAY", 10, 20),
    ]
    topology = ElecTopologyBuilder().build(elements)
    assert topology["summary"]["drawing_intent"] in {"GROUNDING_PLAN", "WIRING_PLAN", "EQUIPMENT_PLAN"}, topology["summary"]
    violations = run_deterministic_checks(elements, {}, topology)
    assert any(v["violation_type"] == "open_circuit_error" for v in violations), violations
    fixes = RevisionAgent().calculate_fix([v for v in violations if v["violation_type"] == "open_circuit_error"], {})
    assert fixes[0]["proposed_fix"]["action"] == "move_entity", fixes
    assert fixes[0]["proposed_fix"]["auto_fix"]["type"] == "BRIDGE_WIRE", fixes


def test_arc_elbow_between_ground_lines_is_not_disconnected() -> None:
    elements = [
        _line("G1", 0, 0, 100, 0, "E-GROUND"),
        _line("G2", 200, 100, 200, 500, "E-GROUND"),
        _arc("A1", 100, 0, 200, 100, "E-GROUND"),
        _text("GT", "접지선 E1 E2", 10, 20),
    ]
    topology = ElecTopologyBuilder().build(elements)
    assert topology.get("arc_bridge_connections"), topology
    assert not topology.get("broken_segments"), topology.get("broken_segments")
    violations = run_deterministic_checks(elements, {}, topology)
    assert not [v for v in violations if v["violation_type"] == "open_circuit_error"], violations


def test_grounding_fgv_annotation_maps_to_nearby_wire() -> None:
    elements = [
        _line("GND_W1", 0, 0, 1000, 0, "LID"),
        _text("GND_TXT1", "FGV 35㎟ 접지선", 500, 120),
        _text("GND_TXT2", "E1 접지봉", 0, 200),
    ]
    topology = ElecTopologyBuilder().build(elements)
    assert topology["summary"]["drawing_intent"] == "GROUNDING_PLAN", topology["summary"]
    runs = topology.get("circuit_runs", [])
    assert any(run.get("cable_sqmm") == 35.0 for run in runs), runs


def test_wire_overlap_duplicate_can_generate_cleanup_fix() -> None:
    elements = [
        _line("W1", 0, 0, 500, 0, "E-GROUND"),
        _line("W2", 0, 0, 500, 0, "E-GROUND"),
        _text("GT", "GROUND E1", 10, 20),
    ]
    topology = ElecTopologyBuilder().build(elements)
    violations = run_deterministic_checks(elements, {}, topology)
    overlaps = [v for v in violations if v["violation_type"] == "wire_overlap"]
    assert overlaps, violations
    fixes = RevisionAgent().calculate_fix(overlaps, {})
    assert fixes[0]["proposed_fix"]["action"] == "cleanup_duplicate", fixes
    assert fixes[0]["proposed_fix"]["auto_fix"]["type"] == "DELETE_DUPLICATE_WIRE", fixes


def test_overlapping_electrical_circles_raise_symbol_overlap() -> None:
    elements = [
        _circle("CIR_A", 0, 0, radius=10, layer="E-DEVICE"),
        _circle("CIR_B", 8, 0, radius=10, layer="E-DEVICE"),
    ]
    topology = {"terminal_candidates": [], "broken_segments": [], "summary": {"drawing_intent": "EQUIPMENT_PLAN"}}
    violations = run_deterministic_checks(elements, {}, topology)
    overlaps = [v for v in violations if v["violation_type"] == "electrical_symbol_overlap"]
    assert overlaps, violations
    assert set(overlaps[0]["target_handles"]) == {"CIR_A", "CIR_B"}, overlaps


def test_nested_symbol_circles_are_not_overlap_violation() -> None:
    elements = [
        _circle("OUTER", 0, 0, radius=64.8, layer="E-GROUND"),
        _circle("INNER", 0, 0, radius=32.4, layer="E-GROUND"),
        _text("TXT", "E1 접지", 90, 0),
    ]
    topology = {"terminal_candidates": [], "broken_segments": [], "summary": {"drawing_intent": "GROUNDING_PLAN"}}
    violations = run_deterministic_checks(elements, {}, topology)
    overlaps = [v for v in violations if v["violation_type"] == "electrical_symbol_overlap"]
    assert not overlaps, violations


def test_annotation_helper_and_border_are_not_wire_candidates() -> None:
    elements = [
        _line("A1", 0, 0, 500, 0, "ANNO"),
        _line("B1", 0, 20, 800, 20, "BORDER"),
        _line("W1", 0, 100, 500, 100, "E-CABLE"),
        _text("WT", "CABLE CV 50mm2", 10, 110),
    ]
    topology = ElecTopologyBuilder().build(elements)
    wf = topology["summary"]["wire_filter"]
    assert "W1" in wf["wire_candidate_handles"], wf
    suppressed = {item["handle"]: item["reason"] for item in wf["suppressed_wire_candidates"]}
    assert suppressed.get("A1") == "excluded_layer", wf
    assert suppressed.get("B1") == "excluded_layer", wf


def test_table_mapping_semantics() -> None:
    report = build_table_mapping_debug([
        _text("C1", "CABLE CV 50mm2", 0, 0),
        _text("C2", "GROUND E1", 0, 10),
        _text("C3", "TEST BOX", 0, 20),
    ])
    assert report["table_count"] == 1, report
    semantics = {m["semantic"] for m in report["mapped_table_annotations"]}
    assert {"cable_spec", "grounding_spec", "test_annotation"} <= semantics, report


def test_wire_connected_orphan_is_suppressed() -> None:
    """wire endpoint에 연결된 원형은 orphan 위반으로 잡히면 안 된다."""
    base = [_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)]
    # 원형 중심(200, 0)에서 radius=9이면 snap=13.5 → 끝점(200, 10)은 10 < 13.5 → 연결됨
    orphan = _circle("ORP_W", 200, 0)
    wire = {"handle": "WW1", "type": "LINE", "layer": "E-WIRE",
            "start": {"x": 200, "y": 10}, "end": {"x": 200, "y": 80}}
    elements = base + [orphan, wire]
    topology = _topology(base)
    violations = run_deterministic_checks(elements, {}, topology)
    assert not [v for v in violations if v["violation_type"] == "terminal_orphan_circle" and v["object_id"] == "ORP_W"], violations


def test_peer_row_pattern_is_suppressed() -> None:
    """동일 Y축에 등간격으로 늘어선 orphan 원형 2개는 반복 패턴으로 억제되어야 한다."""
    base = [_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)]
    # 2개의 orphan이 같은 Y=200, 동일 간격 → peer row pattern
    orphan1 = _circle("OR1", 100, 200)
    orphan2 = _circle("OR2", 140, 200)
    elements = base + [orphan1, orphan2]
    topology = _topology(base)
    violations = run_deterministic_checks(elements, {}, topology)
    assert not [v for v in violations if v["violation_type"] == "terminal_orphan_circle"], violations


def test_orphan_circle_auto_fix_is_delete() -> None:
    """terminal_orphan_circle 위반의 auto_fix type이 DELETE여야 한다 (auto_fix 0건 방지)."""
    base = [_circle("A", 0, 0), _circle("B", 40, 0), _circle("C", 0, 35), _circle("D", 40, 35)]
    orphan = _circle("ORP", 200, 0)
    elements = base + [orphan]
    topology = _topology(base)
    violations = run_deterministic_checks(elements, {}, topology)
    orphan_violations = [v for v in violations if v["violation_type"] == "terminal_orphan_circle"]
    assert orphan_violations, violations

    fixes = RevisionAgent().calculate_fix(orphan_violations, {})
    pf = fixes[0]["proposed_fix"]
    assert pf.get("type") == "DELETE", pf


if __name__ == "__main__":
    test_detached_circle_move_fix()
    test_e1_or_test_circle_is_suppressed()
    test_repeated_test_symbols_do_not_raise_geometry_violation()
    test_e1_e2_grounding_node_cluster_is_not_terminal_deformation()
    test_orphan_circle_auto_fix_is_delete()
    test_detail_drawing_suppresses_circuit_violations()
    test_debug_fields_are_written_to_topology()
    test_grounding_plan_intent_and_disconnected_ground_line()
    test_grounding_fgv_annotation_maps_to_nearby_wire()
    test_wire_overlap_duplicate_can_generate_cleanup_fix()
    test_overlapping_electrical_circles_raise_symbol_overlap()
    test_annotation_helper_and_border_are_not_wire_candidates()
    test_table_mapping_semantics()
    print("PASS: context-aware ELEC geometry QA assertions")
