# Sophia Vector — Sviluppo locale

## Prerequisiti

- Python venv in `.venv` (usare `.venv/bin/python -m <modulo>`, vedi nota sotto)
- Node 22 + npm (per il frontend)
- Servizi esterni attivi a seconda di cosa si testa:
  - UI gestione (vector store, sources): **Qdrant** + **MongoDB**
  - Ingestion: anche **Docling** (`:5001`) + **BGE-M3** (`:8004`)
  - Search: **BGE-M3** + **Qdrant**

## Setup

```bash
# Dipendenze backend
.venv/bin/python -m pip install -r requirements.txt

# Dipendenze frontend
(cd frontend && npm install)

# Config: copia il template e setta almeno NEXTAUTH_SECRET
cp -n .env.example .env
#   NEXTAUTH_SECRET=$(openssl rand -base64 32)
```

## Avvio dev

```bash
./dev-start.sh
```

- Backend → http://localhost:8100  (log `.data/dev-logs/backend.log`)
- Frontend → http://localhost:3100 (log `.data/dev-logs/frontend.log`)
- Primo login sulla UI = admin.

## Nota venv (importante)

Gli script wrapper del venv (`.venv/bin/uvicorn`, `.venv/bin/pip`) hanno uno shebang con path
assoluto e **si rompono** perché il progetto è stato spostato di cartella. Usare sempre
`.venv/bin/python -m uvicorn …` / `.venv/bin/python -m pip …`. `dev-start.sh` lo fa già.
Le URL e le altre opzioni si configurano via env con prefisso `SOPHIA_VECTOR_` (vedi `.env.example`).
