"""
Fine-tuning di GLiNER (gliner-community/gliner_small-v2.5) sul dominio bancario IT.

Prerequisiti:
    pip install gliner -U
    pip install accelerate

Hardware: gira su CPU (lento) o GPU modesta. Un small (~166M param) si fine-tuna
tranquillamente su una qualsiasi GPU; con la tua 96-180GB è una passeggiata.

NB: questo è lo SCHELETRO. La parte vera è costruire `train_data` dai tuoi dati
(vedi sezione 1). Senza dati annotati di qualità, il fine-tuning non serve a nulla.
"""

import json
import random
from gliner import GLiNER
from gliner.training import Trainer, TrainingArguments
from gliner.data_processing.collator import DataCollator


# ============================================================
# 1. IL DATASET — la parte che conta davvero (il 90% del lavoro)
# ============================================================
#
# GLiNER vuole ogni esempio in questo formato:
#   {
#     "tokenized_text": ["La", "banca", "deve", "rispondere", "entro", "15", "giorni", "."],
#     "ner": [ [start_tok, end_tok, "label"], ... ]   # indici INCLUSIVI sui token
#   }
#
# Esempio reale del tuo dominio:
#   testo: "La Funzione Compliance verifica i tassi soglia entro 60 giorni."
#   tokenized_text: ["La","Funzione","Compliance","verifica","i","tassi","soglia","entro","60","giorni","."]
#   ner: [ [1, 2, "funzione di controllo"],   # "Funzione Compliance" = token 1..2
#          [8, 9, "termine temporale"] ]       # "60 giorni" = token 8..9
#
# ATTENZIONE (gli errori classici):
# - gli indici sono sui TOKEN, non sui caratteri. Se tokenizzi male, le entità si spostano.
# - start/end sono INCLUSIVI: "Funzione Compliance" su token 1 e 2 → [1, 2], non [1, 3].
# - la stessa label deve essere scritta SEMPRE uguale ("funzione di controllo", non a volte "Funzione").
# - servono anche esempi NEGATIVI (frasi senza entità di quel tipo) → ner: []
#   altrimenti il modello impara a vedere entità ovunque.

def tokenize(text: str):
    """Tokenizzazione semplice a parole. Per il vero training valuta uno
    tokenizer più robusto (es. quello di spacy 'it_core_news_sm'), ma deve
    essere COERENTE tra costruzione dataset e inferenza."""
    # split molto naive: separa la punteggiatura di fine parola
    import re
    return re.findall(r"\w+|[^\w\s]", text, re.UNICODE)


# --- ESEMPIO di costruzione del dataset (qui hardcoded, tu lo genererai dai tuoi dati) ---
# NEL TUO CASO: questi esempi NON li scrivi a mano da zero. Li derivi dai tuoi
# dati RGCI già etichettati:
#   - le `obligations`/`controls` con `testo_originale` verbatim → esempi positivi
#   - gli scarti audit (`discard_item`) → esempi negativi
# Devi solo trovare la posizione (start/end token) dell'entità dentro il testo.

raw_examples = [
    ("La Funzione Compliance verifica i tassi soglia entro 60 giorni.",
     [("Funzione Compliance", "funzione di controllo"), ("60 giorni", "termine temporale")]),
    ("Il presente regolamento entra in vigore il 17 febbraio 2026.",
     [("17 febbraio 2026", "data di applicazione")]),
    ("La banca trasmette la documentazione all'organo di vigilanza.",
     []),  # esempio NEGATIVO: nessuna entità dei tipi che ci interessano
]


def build_example(text: str, spans: list) -> dict:
    """Converte (testo, [(stringa_entità, label)]) nel formato GLiNER (indici token)."""
    tokens = tokenize(text)
    ner = []
    for entity_str, label in spans:
        ent_tokens = tokenize(entity_str)
        # trova la sottosequenza di token dell'entità dentro i token del testo
        for i in range(len(tokens) - len(ent_tokens) + 1):
            if tokens[i:i + len(ent_tokens)] == ent_tokens:
                ner.append([i, i + len(ent_tokens) - 1, label])  # end INCLUSIVO
                break
    return {"tokenized_text": tokens, "ner": ner}


dataset = [build_example(t, s) for t, s in raw_examples]
# In produzione: migliaia di questi, generati dai tuoi dati RGCI.

random.shuffle(dataset)
split = int(len(dataset) * 0.9)
train_data = dataset[:split]
eval_data = dataset[split:] or dataset[:1]  # guard se il dataset è minuscolo

print(f"Train: {len(train_data)} · Eval: {len(eval_data)}")


# ============================================================
# 2. CARICA IL MODELLO BASE
# ============================================================
model = GLiNER.from_pretrained("gliner-community/gliner_small-v2.5", load_tokenizer=True)


# ============================================================
# 3. CONFIGURA IL TRAINING
# ============================================================
data_collator = DataCollator(
    model.config,
    data_processor=model.data_processor,
    prepare_labels=True,
)

training_args = TrainingArguments(
    output_dir="models/gliner-banking-it",
    learning_rate=5e-6,           # GLiNER vuole LR bassi; 5e-6..1e-5 tipico per fine-tuning
    weight_decay=0.01,
    others_lr=1e-5,               # LR per la testa (più alto del backbone)
    others_weight_decay=0.01,
    lr_scheduler_type="linear",
    warmup_ratio=0.1,
    per_device_train_batch_size=8,
    per_device_eval_batch_size=8,
    num_train_epochs=10,          # pochi esempi → più epoche; tanti esempi → meno
    eval_strategy="steps",
    eval_steps=100,
    save_steps=200,
    save_total_limit=2,
    dataloader_num_workers=0,
    use_cpu=False,                # True se non hai GPU
    report_to="none",
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_data,
    eval_dataset=eval_data,
    tokenizer=model.data_processor.transformer_tokenizer,
    data_collator=data_collator,
)


# ============================================================
# 4. ADDESTRA
# ============================================================
if __name__ == "__main__":
    trainer.train()
    model.save_pretrained("models/gliner-banking-it-final")
    print("✅ salvato in models/gliner-banking-it-final")

    # ---- test rapido ----
    tuned = GLiNER.from_pretrained("models/gliner-banking-it-final", load_tokenizer=True)
    txt = "La Funzione Risk Management adotta le misure entro 30 giorni dalla pubblicazione."
    labels = ["funzione di controllo", "termine temporale", "data di applicazione"]
    for e in tuned.predict_entities(txt, labels):
        print(f"  {e['text']} => {e['label']} ({e['score']:.2f})")
