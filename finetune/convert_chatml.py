"""
File    : finetune/convert_chatml.py
Author  : 김다빈
WBS     : FT-01 (SFT 데이터셋 구축)
Create  : 2026-04-15

Description :
    ft01_labels/ 의 (입력 CAD JSON, 출력 violations) 쌍을
    Qwen3 학습용 ChatML JSONL 형식으로 변환한다.

    출력 형식 (ChatML):
        {"messages": [
            {"role": "system",    "content": "..."},
            {"role": "user",      "content": "CAD JSON 포함"},
            {"role": "assistant", "content": "annotated_entities JSON"}
        ]}

    사용법:
        python finetune/convert_chatml.py --domain arch
        python finetune/convert_chatml.py --domain arch --split 0.9
"""

import argparse
import json
import random
from pathlib import Path

# ── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent.parent
LABELS_DIR  = ROOT / "ft01_labels"
OUTPUT_DIR  = ROOT / "ft01_chatml"

# ── 도메인별 시스템 프롬프트 (학습용, 간결하게) ───────────────────────────────
SYSTEM_PROMPTS = {
    "arch": (
        "당신은 건축법 전문 도면 검토 AI입니다. "
        "CAD JSON 도면 데이터를 분석하여 건축법 위반 항목을 찾고, "
        "반드시 JSON 형식으로만 응답하세요.\n"
        "{\"annotated_entities\": [...], \"summary\": \"...\"}"
    ),
    "elec": (
        "당신은 KEC(한국전기설비규정) 전문 도면 검토 AI입니다. "
        "CAD JSON 도면 데이터를 분석하여 KEC 위반 항목을 찾고, "
        "반드시 JSON 형식으로만 응답하세요.\n"
        "{\"annotated_entities\": [...], \"summary\": \"...\"}"
    ),
    "pipe": (
        "당신은 KGS 배관 설비 전문 도면 검토 AI입니다. "
        "CAD JSON 도면 데이터를 분석하여 KGS 위반 항목을 찾고, "
        "반드시 JSON 형식으로만 응답하세요.\n"
        "{\"annotated_entities\": [...], \"summary\": \"...\"}"
    ),
    "fire": (
        "당신은 화재안전기준(NFSC) 전문 도면 검토 AI입니다. "
        "CAD JSON 도면 데이터를 분석하여 NFSC 위반 항목을 찾고, "
        "반드시 JSON 형식으로만 응답하세요.\n"
        "{\"annotated_entities\": [...], \"summary\": \"...\"}"
    ),
}


def build_user_message(cad_json: dict) -> str:
    return (
        "다음 CAD 도면 데이터를 검토해주세요:\n\n"
        + json.dumps(cad_json, ensure_ascii=False)
    )


def build_assistant_message(output: dict) -> str:
    return json.dumps(output, ensure_ascii=False)


def convert(label_files: list, domain: str) -> list:
    system = SYSTEM_PROMPTS[domain]
    samples = []

    for path in label_files:
        try:
            pair = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [읽기 오류] {path.name}: {e}")
            continue

        cad_json = pair.get("input")
        output   = pair.get("output")
        if not cad_json or not output:
            print(f"  [스킵] 입출력 누락: {path.name}")
            continue

        sample = {
            "messages": [
                {"role": "system",    "content": system},
                {"role": "user",      "content": build_user_message(cad_json)},
                {"role": "assistant", "content": build_assistant_message(output)},
            ]
        }
        samples.append(sample)

    return samples


def main():
    parser = argparse.ArgumentParser(description="FT-01 ChatML 변환")
    parser.add_argument("--domain", required=True, choices=["arch", "elec", "pipe", "fire"])
    parser.add_argument("--split",  default=0.9, type=float, help="train/eval 분할 비율 (기본: 0.9)")
    parser.add_argument("--seed",   default=42,  type=int)
    args = parser.parse_args()

    label_files = sorted(LABELS_DIR.glob(f"*_label.json"))
    if not label_files:
        print(f"[오류] ft01_labels/ 에 라벨 파일 없음. generate_labels.py 먼저 실행하세요.")
        return

    print(f"[변환] 도메인={args.domain} | 라벨 파일={len(label_files)}개")

    samples = convert(label_files, args.domain)
    if not samples:
        print("[오류] 변환된 샘플 없음.")
        return

    # train / eval 분할
    random.seed(args.seed)
    random.shuffle(samples)
    split_idx  = int(len(samples) * args.split)
    train_data = samples[:split_idx]
    eval_data  = samples[split_idx:]

    OUTPUT_DIR.mkdir(exist_ok=True)
    train_path = OUTPUT_DIR / f"{args.domain}_train.jsonl"
    eval_path  = OUTPUT_DIR / f"{args.domain}_eval.jsonl"

    def write_jsonl(path, data):
        with open(path, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    write_jsonl(train_path, train_data)
    write_jsonl(eval_path,  eval_data)

    print(f"\n[완료]")
    print(f"  train: {len(train_data)}건 → {train_path}")
    print(f"  eval : {len(eval_data)}건  → {eval_path}")
    print(f"\n다음 단계: python finetune/train_qlora.py --domain {args.domain}")


if __name__ == "__main__":
    main()
