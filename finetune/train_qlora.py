"""
File    : finetune/train_qlora.py
Author  : 김다빈
WBS     : FT-02 (QLoRA 파인튜닝 환경 구성)
Create  : 2026-04-15

Description :
    ft01_chatml/{domain}_train.jsonl 을 Qwen3 기반 모델에
    QLoRA(4-bit)로 파인튜닝한다.

    환경 요구사항 (RunPod GPU 서버):
        pip install transformers peft trl bitsandbytes accelerate datasets

    사용법:
        python finetune/train_qlora.py --domain arch
        python finetune/train_qlora.py --domain arch --base_model Qwen/Qwen3-72B-Instruct
        python finetune/train_qlora.py --domain arch --epochs 3 --batch_size 2
"""

import argparse
from pathlib import Path

# ── 경로 설정 ────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
CHATML_DIR = ROOT / "ft01_chatml"
OUTPUT_DIR = ROOT / "ft02_models"

DOMAIN_LABEL = {
    "arch": "건축",
    "elec": "전기",
    "pipe": "배관",
    "fire": "소방",
}


def train(args):
    # 학습 데이터 경로
    train_path = CHATML_DIR / f"{args.domain}_train.jsonl"
    eval_path  = CHATML_DIR / f"{args.domain}_eval.jsonl"

    if not train_path.exists():
        print(f"[오류] 학습 데이터 없음: {train_path}")
        print("convert_chatml.py 먼저 실행하세요.")
        return

    output_path = OUTPUT_DIR / f"qwen3-{args.domain}-qlora"
    OUTPUT_DIR.mkdir(exist_ok=True)

    # ── 라이브러리 임포트 (GPU 환경에서만 실행) ──────────────────────────────
    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            TrainingArguments,
        )
        from trl import SFTTrainer
    except ImportError as e:
        print(f"[오류] 필수 라이브러리 없음: {e}")
        print("pip install transformers peft trl bitsandbytes accelerate datasets")
        return

    print(f"[FT-02] 도메인={DOMAIN_LABEL[args.domain]} | 모델={args.base_model}")
    print(f"[FT-02] train={train_path} | output={output_path}\n")

    # ── 4-bit 양자화 설정 ────────────────────────────────────────────────────
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )

    # ── 모델 + 토크나이저 로드 ───────────────────────────────────────────────
    print("[1/4] 모델 로드 중...")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)

    # ── LoRA 설정 ────────────────────────────────────────────────────────────
    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── 데이터셋 로드 ────────────────────────────────────────────────────────
    print("[2/4] 데이터셋 로드 중...")
    data_files = {"train": str(train_path)}
    if eval_path.exists():
        data_files["validation"] = str(eval_path)

    dataset = load_dataset("json", data_files=data_files)

    def format_messages(example):
        return tokenizer.apply_chat_template(
            example["messages"],
            tokenize=False,
            add_generation_prompt=False,
        )

    # ── 학습 설정 ────────────────────────────────────────────────────────────
    print("[3/4] 학습 시작...")
    training_args = TrainingArguments(
        output_dir=str(output_path),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        fp16=False,
        bf16=True,
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch" if eval_path.exists() else "no",
        load_best_model_at_end=eval_path.exists(),
        report_to="none",
        dataloader_num_workers=0,
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset.get("validation"),
        tokenizer=tokenizer,
        formatting_func=format_messages,
        max_seq_length=args.max_length,
        packing=False,
    )

    trainer.train()

    # ── 모델 저장 ────────────────────────────────────────────────────────────
    print("[4/4] 모델 저장 중...")
    trainer.save_model(str(output_path))
    tokenizer.save_pretrained(str(output_path))

    print(f"\n[FT-02 완료] 저장 경로: {output_path}")
    print("추론 테스트: python finetune/inference_test.py --domain {args.domain}")


def main():
    parser = argparse.ArgumentParser(description="FT-02 QLoRA 파인튜닝")
    parser.add_argument("--domain",     required=True, choices=["arch", "elec", "pipe", "fire"])
    parser.add_argument("--base_model", default="Qwen/Qwen3-72B-Instruct")
    parser.add_argument("--epochs",     default=3,    type=int)
    parser.add_argument("--batch_size", default=1,    type=int)
    parser.add_argument("--max_length", default=8192, type=int)
    args = parser.parse_args()

    train(args)


if __name__ == "__main__":
    main()
