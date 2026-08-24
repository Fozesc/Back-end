# Back-end — Fozesc

API Flask do sistema Fozesc (factoring / borderôs). A **mesma base de código** roda
local e em produção — a diferença é só de onde vêm as variáveis de ambiente.

## 🔀 Como trocar entre LOCAL e DEPLOY (sem mexer no código)

A conexão com o banco é decidida por variáveis de ambiente
(`DATABASE_URL` e `JWT_SECRET_KEY`). Elas são lidas assim:

1. **Produção (Docker):** vêm do `docker-compose` (a partir do `.env` do servidor).
   O `.env` local **não existe** dentro do container (está no `.dockerignore`).
2. **Local (sua máquina):** vêm do arquivo `Back-end/.env`.

`load_dotenv(override=False)` garante que, se a variável já existir no ambiente
(produção), o `.env` **não** a sobrescreve. Ou seja: **o ambiente sempre vence**,
então nada do seu local interfere no servidor.

## ▶️ Rodar LOCAL

Pré-requisito: um Postgres acessível (ex.: o container `fozesc_db` em `localhost:5433`).

```bash
cd Back-end
cp .env.example .env          # 1ª vez: cria seu .env local e ajuste os valores
./venv/bin/python run.py      # sobe em http://localhost:5001
```

Frontend (noutro terminal):
```bash
cd Front-end
npm install
npm run dev                   # http://localhost:5173 (proxy /api -> :5001)
```

## 🚀 Deploy (servidor)

O servidor usa o `docker-compose.yml` da raiz + um `.env` na raiz
(veja `.env.example` da raiz: `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `JWT_SECRET`,
`VITE_API_URL`).

```bash
# no servidor, na raiz do projeto:
cp .env.example .env          # preencha com os valores reais (1ª vez)
docker-compose up --build -d  # sobe banco + backend + frontend
```

O compose já injeta a `DATABASE_URL` (apontando pro container do Postgres) e a
`JWT_SECRET_KEY` no backend automaticamente — **não precisa** criar `Back-end/.env`
no servidor.

## 📌 Resumo

| | Local | Deploy (servidor) |
|---|---|---|
| Onde ficam as variáveis | `Back-end/.env` | `.env` da raiz (docker-compose) |
| Como sobe | `./venv/bin/python run.py` | `docker-compose up --build -d` |
| Precisa mexer no código? | Não | Não |
