import os
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv

# ==========================================================================
# CONEXÃO COM O BANCO — funciona em 2 modos SEM mexer no código:
#
#   PRODUÇÃO (deploy / Docker):
#     As variáveis DATABASE_URL e JWT_SECRET_KEY vêm do ambiente
#     (docker-compose `environment:` / .env do servidor). O arquivo .env LOCAL
#     NÃO existe dentro do container (está no .dockerignore), então nada da sua
#     máquina interfere no servidor.
#
#   LOCAL (sua máquina):
#     As mesmas variáveis são lidas do arquivo Back-end/.env.
#     `override=False` garante que, se a variável já existir no ambiente
#     (produção), o .env NÃO a sobrescreve. Logo o ambiente SEMPRE vence.
#
#   >>> PARA TROCAR entre local e deploy você NÃO edita código:
#       - Local:  ajuste o Back-end/.env  (veja Back-end/.env.example)
#       - Deploy: o servidor usa o próprio ambiente (docker-compose)
# ==========================================================================
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / '.env', override=False)


class Config:
    # Lê primeiro do AMBIENTE (produção); no local vem do .env carregado acima.
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    SECRET_KEY = os.getenv('JWT_SECRET_KEY')

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Tempo de expiração do login (1 semana).
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=7)

    if not SQLALCHEMY_DATABASE_URI:
        raise ValueError(
            "DATABASE_URL não definida. Em produção ela vem do docker-compose; "
            "no local, copie Back-end/.env.example para Back-end/.env e preencha."
        )

    if not SECRET_KEY:
        raise ValueError(
            "JWT_SECRET_KEY não definida. Em produção ela vem do docker-compose; "
            "no local, defina JWT_SECRET_KEY no Back-end/.env."
        )
