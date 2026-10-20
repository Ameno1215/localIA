"""Entraîne un adaptateur LoRA (QLoRA 4 bits, Unsloth) sur data/dataset.jsonl.

Tient dans 8 Go de VRAM avec un 4B en QLoRA (base : models/Qwen3.5-4B, téléchargée par 01-telecharger-modeles.sh). Il faut libérer le GPU avant :
  docker compose stop vllm
  .venv-finetune/bin/python finetune/entrainer_lora.py
Sortie : loras/normes/  (servi ensuite par vLLM sous le nom de modèle "normes")
Sur le Spark : même script, changer BASE pour le modèle de prod.
"""

import os

from unsloth import FastLanguageModel  # doit être importé avant trl/transformers
from unsloth.chat_templates import train_on_responses_only
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer

BASE = os.environ.get("BASE", "models/Qwen3.5-4B")  # même modèle que VLLM_MODEL, en BF16
SORTIE = os.environ.get("SORTIE", "loras/normes")
EPOQUES = float(os.environ.get("EPOQUES", "2"))

model, tokenizer = FastLanguageModel.from_pretrained(BASE, max_seq_length=2048, load_in_4bit=True)
model = FastLanguageModel.get_peft_model(
    model,
    r=16,  # doit rester <= --max-lora-rank côté vLLM (32)
    lora_alpha=32,
    lora_dropout=0,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    use_gradient_checkpointing="unsloth",
    random_state=42,
)

ds = load_dataset("json", data_files="data/dataset.jsonl", split="train")
ds = ds.map(lambda ex: {"text": tokenizer.apply_chat_template(ex["messages"], tokenize=False)})
ds = ds.train_test_split(test_size=0.05, seed=42)

trainer = SFTTrainer(
    model=model,
    processing_class=tokenizer,
    train_dataset=ds["train"],
    eval_dataset=ds["test"],
    args=SFTConfig(
        dataset_text_field="text",
        max_length=2048,
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        num_train_epochs=EPOQUES,
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        optim="adamw_8bit",
        bf16=True,
        logging_steps=10,
        eval_strategy="epoch",
        save_strategy="no",
        output_dir="data/checkpoints",
        report_to="none",
    ),
)
# On n'apprend que sur les réponses, pas sur les questions
trainer = train_on_responses_only(
    trainer, instruction_part="<|im_start|>user\n", response_part="<|im_start|>assistant\n"
)
trainer.train()

model.save_pretrained(SORTIE)
tokenizer.save_pretrained(SORTIE)
print(f"Adaptateur LoRA sauvegardé dans {SORTIE}")
