import websocket
import json

ws = websocket.create_connection("ws://localhost:8000/ws/cad")

# 1. 검토 결과 시뮬레이션 (구름마크 생성)
review_msg = {
    "action": "REVIEW_RESULT",
    "payload": {
        "annotated_entities": [{
            "handle": "fa7", # 정확하게 fa7 매칭됨
            "violation": {
                "id": "VOL-001",
                "rule": "KDS 31-10",
                "auto_fix": {"type": "MOVE", "delta_x": 100.0, "delta_y": 0.0} # 테스트니까 100만큼만 우측으로 이동
            },
            "bbox": {"x1": 552.0, "y1": 42.0, "x2": 652.0, "y2": 387.0} # 선을 감싸는 실제 여유 좌표
        }]
    }
}
ws.send(json.dumps(review_msg))
print("구름마크 생성 명령 전송 완료!")

# 2. 승인 명령 시뮬레이션 (실제 도면 수정)
approve_msg = {
    "action": "APPROVE_FIX",
    "payload": {"violation_id": "VOL-001"}
}
ws.send(json.dumps(approve_msg))
  