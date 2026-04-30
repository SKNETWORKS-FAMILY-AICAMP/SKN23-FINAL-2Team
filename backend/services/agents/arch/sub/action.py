"""
File    : backend/services/agents/architecture/sub/action.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15

Description :
    RevisionAgent의 수정 결과를 C# DrawingPatcher 제어 명령 JSON으로 직렬화.
    handle 기반으로 AutoCAD 엔티티를 직접 수정합니다.

    DrawingPatcher AutoFix 타입 (12종):
        MOVE, SCALE, DELETE, LAYER, ATTRIBUTE, TEXT_CONTENT,
        TEXT_HEIGHT, COLOR, LINETYPE, LINEWEIGHT, ROTATE, GEOMETRY
"""

import json

from backend.services.agents.arch.schemas import RevisionAction


class ActionAgent:
    def generate_command(self, modifications: list) -> str:
        """
        Parameters
        ----------
        modifications : list of {handle, violation_type, proposed_fix}

        Returns
        -------
        str — JSON: {"actions": [...]}
        각 action은 DrawingPatcher가 소비하는 형태.
        """
        commands = []

        for mod in modifications:
            handle       = mod.get("handle")
            proposed_fix = mod.get("proposed_fix", {})
            action       = proposed_fix.get("action")

            if action == RevisionAction.MOVE:
                commands.append({
                    "command_type": "MOVE",
                    "handle": handle,
                    "parameters": {
                        "reason":         proposed_fix.get("reason"),
                        "current_value":  proposed_fix.get("current_value"),
                        "required_value": proposed_fix.get("required_value"),
                        "note":           proposed_fix.get("note", ""),
                    },
                })

            elif action == RevisionAction.SCALE:
                commands.append({
                    "command_type": "SCALE",
                    "handle": handle,
                    "parameters": {
                        "scale_factor":   proposed_fix.get("scale_factor", 1.0),
                        "current_value":  proposed_fix.get("current_value"),
                        "required_value": proposed_fix.get("required_value"),
                        "reason":         proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.DELETE:
                commands.append({
                    "command_type": "DELETE",
                    "handle": handle,
                    "parameters": {
                        "reason": proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.LAYER:
                commands.append({
                    "command_type": "LAYER",
                    "handle": handle,
                    "parameters": {
                        "new_layer":      proposed_fix.get("new_layer"),
                        "current_layer":  proposed_fix.get("current_layer"),
                        "reason":         proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.ATTRIBUTE:
                commands.append({
                    "command_type": "ATTRIBUTE",
                    "handle": handle,
                    "parameters": {
                        "key":       proposed_fix.get("key"),
                        "new_value": proposed_fix.get("new_value"),
                        "old_value": proposed_fix.get("old_value"),
                        "reason":    proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.TEXT_CONTENT:
                commands.append({
                    "command_type": "TEXT_CONTENT",
                    "handle": handle,
                    "parameters": {
                        "new_text": proposed_fix.get("new_text"),
                        "reason":   proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.TEXT_HEIGHT:
                commands.append({
                    "command_type": "TEXT_HEIGHT",
                    "handle": handle,
                    "parameters": {
                        "new_height": proposed_fix.get("new_height"),
                        "reason":     proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.COLOR:
                commands.append({
                    "command_type": "COLOR",
                    "handle": handle,
                    "parameters": {
                        "new_color": proposed_fix.get("new_color"),
                        "reason":    proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.LINETYPE:
                commands.append({
                    "command_type": "LINETYPE",
                    "handle": handle,
                    "parameters": {
                        "new_linetype": proposed_fix.get("new_linetype"),
                        "reason":       proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.LINEWEIGHT:
                commands.append({
                    "command_type": "LINEWEIGHT",
                    "handle": handle,
                    "parameters": {
                        "new_lineweight": proposed_fix.get("new_lineweight"),
                        "reason":         proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.ROTATE:
                commands.append({
                    "command_type": "ROTATE",
                    "handle": handle,
                    "parameters": {
                        "angle_deg": proposed_fix.get("angle_deg"),
                        "reason":    proposed_fix.get("reason"),
                    },
                })

            elif action == RevisionAction.GEOMETRY:
                commands.append({
                    "command_type": "GEOMETRY",
                    "handle": handle,
                    "parameters": {
                        "current_value":  proposed_fix.get("current_value"),
                        "required_value": proposed_fix.get("required_value"),
                        "reason":         proposed_fix.get("reason"),
                        "note":           proposed_fix.get("note", ""),
                    },
                })

            else:  # MANUAL_REVIEW or unknown
                commands.append({
                    "command_type": "HIGHLIGHT_WARNING",
                    "handle": handle,
                    "parameters": {
                        "message": proposed_fix.get("reason", "수동 검토 필요"),
                    },
                })

        return json.dumps({"actions": commands}, ensure_ascii=False)
