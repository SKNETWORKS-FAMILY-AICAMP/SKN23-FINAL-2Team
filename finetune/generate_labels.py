"""
File    : finetune/generate_labels.py
Author  : 김다빈
WBS     : FT-01 (SFT 데이터셋 구축)
Create  : 2026-04-15

Description :
    ft01_dataset/ 의 CAD JSON 파일들을 GPT-4로 검토하여
    (입력 CAD JSON, 출력 violations) 쌍을 생성한다.

    법규 파일 포맷:
        .md  → 전체 텍스트를 그대로 사용
        .json → [{doc_name, domain, category, section_id, chunk_type, content, ...}, ...]
                image 전용 chunk(![]로 시작) 필터링 후 content 연결

    실행 순서:
        1. ft01_dataset/*.json 로드
        2. 도메인별 법규 텍스트 + CAD JSON → GPT-4 호출
        3. annotated_entities 형식의 라벨 저장 (ft01_labels/*.json)

    사용법:
        python finetune/generate_labels.py --domain arch --law_file laws/arch.json
        python finetune/generate_labels.py --domain arch --law_file laws/arch.md
        python finetune/generate_labels.py --domain elec --law_file laws/elec.json
        python finetune/generate_labels.py --domain pipe --law_file laws/pipe.json
        python finetune/generate_labels.py --domain fire --law_file laws/fire.json
"""

import argparse
import json
import re
import time
from pathlib import Path

from openai import OpenAI

# ── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent.parent
INPUT_DIR   = ROOT / "ft01_dataset"
OUTPUT_DIR  = ROOT / "ft01_labels"

# ── 도메인별 시스템 프롬프트 ─────────────────────────────────────────────────
SYSTEM_PROMPTS = {
    "arch": "당신은 건축법 전문 도면 검토 AI입니다. 주어진 CAD JSON 도면 데이터를 건축법 시행령 기준으로 검토하여 위반 항목을 찾아내세요.",
    "elec": "당신은 한국전기설비규정(KEC) 전문 도면 검토 AI입니다. 주어진 CAD JSON 도면 데이터를 KEC 기준으로 검토하여 위반 항목을 찾아내세요.",
    "pipe": "당신은 KGS 배관 설비 전문 도면 검토 AI입니다. 주어진 CAD JSON 도면 데이터를 KGS 기준으로 검토하여 위반 항목을 찾아내세요.",
    "fire": "당신은 화재안전기준(NFSC) 전문 도면 검토 AI입니다. 주어진 CAD JSON 도면 데이터를 NFSC 기준으로 검토하여 위반 항목을 찾아내세요.",
}

OUTPUT_FORMAT = """
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.

{
  "annotated_entities": [
    {
      "handle": "엔티티 handle 값 (CAD JSON의 handle 필드 그대로)",
      "type": "엔티티 type 값",
      "layer": "엔티티 layer 값",
      "bbox": {"x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.0},
      "violation": {
        "id": "V001",
        "severity": "Critical | Major | Minor",
        "rule": "법규 조항 (예: 건축법 시행령 제46조 제1항)",
        "description": "위반 내용 구체적 설명 (수치 포함)",
        "suggestion": "수정 방법",
        "auto_fix": {
          "type": "MOVE | SCALE | DELETE | LAYER | ATTRIBUTE | TEXT_CONTENT | TEXT_HEIGHT | COLOR | LINETYPE | LINEWEIGHT | ROTATE | GEOMETRY",
          "// type별 추가 파라미터 포함": ""
        }
      }
    }
  ],
  "summary": "전체 검토 결과 요약"
}

위반이 없으면 annotated_entities를 빈 배열로 반환하세요.
"""


_IMAGE_PATTERN = re.compile(r"^!\[.*?\]\(.*?\)")


def load_law_text(law_path: Path) -> str:
    """
    법규 파일 로드.
    - .md  : 전체 텍스트 반환
    - .json: chunk 배열에서 content 추출, 이미지 전용 chunk 제거 후 연결
    """
    raw = law_path.read_text(encoding="utf-8")

    if law_path.suffix.lower() == ".json":
        chunks = json.loads(raw)
        texts = []
        current_section = None
        for chunk in chunks:
            content = chunk.get("content", "").strip()
            # 이미지만 있는 chunk 건너뜀
            if not content or _IMAGE_PATTERN.match(content):
                continue
            # 이미지 인라인 참조 제거 (텍스트 내 포함된 경우)
            content = re.sub(r"!\[.*?\]\(.*?\)", "", content).strip()
            if not content:
                continue

            section = chunk.get("section_id") or chunk.get("category", "")
            if section and section != current_section:
                texts.append(f"\n[{section}]")
                current_section = section
            texts.append(content)

        return "\n".join(texts)

    # .md 또는 기타: 그대로 반환
    return raw


def build_user_prompt(cad_json: dict, law_text: str) -> str:
    cad_str = json.dumps(cad_json, ensure_ascii=False)
    return f"""[관련 법규]
{law_text}

[CAD 도면 데이터]
{cad_str}

위 도면을 법규 기준으로 검토하고 지정된 JSON 형식으로 결과를 반환하세요."""


def call_gpt(client: OpenAI, system: str, user: str, model: str = "gpt-5.2") -> dict | None:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        print(f"  [GPT 오류] {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="FT-01 GPT 라벨 생성")
    parser.add_argument("--domain",   required=True, choices=["arch", "elec", "pipe", "fire"])
    parser.add_argument("--law_file", required=True, help="법규 텍스트 파일 경로")
    parser.add_argument("--model",    default="gpt-5.2", help="GPT 모델 (기본: gpt-5.2)")
    parser.add_argument("--delay",    default=1.0, type=float, help="API 호출 간격(초)")
    args = parser.parse_args()

    # 법규 텍스트 로드 (.md → 전체, .json → chunk content 연결)
    law_path = Path(args.law_file)
    if not law_path.exists():
        print(f"[오류] 법규 파일 없음: {law_path}")
        return
    law_text = load_law_text(law_path)
    print(f"[법규] {law_path.name} ({law_path.suffix}) | 텍스트 길이={len(law_text):,}자")

    # 입출력 디렉토리
    if not INPUT_DIR.exists():
        print(f"[오류] ft01_dataset/ 없음. DWG에서 CAD JSON을 먼저 추출하세요.")
        return
    OUTPUT_DIR.mkdir(exist_ok=True)

    json_files = sorted(INPUT_DIR.glob("*.json"))
    if not json_files:
        print(f"[오류] ft01_dataset/ 에 JSON 파일 없음.")
        return

    client = OpenAI()  # OPENAI_API_KEY 환경변수 필요
    system_prompt = SYSTEM_PROMPTS[args.domain] + "\n\n" + OUTPUT_FORMAT

    print(f"[FT-01] 도메인={args.domain} | 파일 수={len(json_files)} | 모델={args.model}")
    print(f"[FT-01] 출력 경로: {OUTPUT_DIR}\n")

    success, skip, fail = 0, 0, 0

    for i, json_path in enumerate(json_files, 1):
        out_path = OUTPUT_DIR / f"{json_path.stem}_label.json"

        # 이미 생성된 파일은 건너뜀
        if out_path.exists():
            print(f"[{i}/{len(json_files)}] SKIP {json_path.name}")
            skip += 1
            continue

        print(f"[{i}/{len(json_files)}] 처리 중: {json_path.name}")

        try:
            cad_json = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [JSON 읽기 오류] {e}")
            fail += 1
            continue

        user_prompt = build_user_prompt(cad_json, law_text)
        result = call_gpt(client, system_prompt, user_prompt, args.model)

        if result is None:
            fail += 1
            continue

        # 입력 + 출력 쌍으로 저장
        pair = {
            "source_file": json_path.name,
            "domain": args.domain,
            "input":  cad_json,
            "output": result,
        }
        out_path.write_text(json.dumps(pair, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → 저장: {out_path.name} | 위반 {len(result.get('annotated_entities', []))}건")
        success += 1
        time.sleep(args.delay)

    print(f"\n[FT-01 완료] 성공={success} | 스킵={skip} | 실패={fail}")
    print(f"다음 단계: python finetune/convert_chatml.py --domain {args.domain}")


if __name__ == "__main__":
    main()
