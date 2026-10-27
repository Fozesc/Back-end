#!/usr/bin/env python3
"""Manda por e-mail, cifrada, uma cópia do backup mais recente de um sistema.

É a cópia fora do servidor: se a máquina estragar, o backup está no Gmail.
Só usa a biblioteca padrão e o `openssl`. É o mesmo script do Smaug.

Na fozesc roda sozinho no container de backup, logo depois do backup das 15h
(gerar_backup.py), lendo as variáveis do ambiente (o docker-compose passa as do .env).
Para testar na hora:

    docker compose exec backup_fozesc python enviar_backups.py

Fora do Docker (como no Smaug, pelo cron), passe o .env do sistema:

    python3 enviar_backups.py /home/lucas/fozesc/.env

Variáveis no .env (só letras, números e hífen nos valores):

    BACKUP_NOME=Fozesc
    BACKUP_PASTA=/home/lucas/fozesc/volumes/backups    (no container: /app/backups)
    BACKUP_EMAIL=para.quem.vai@gmail.com        (vários: separe por vírgula)
    BACKUP_SMTP_USER=conta.que.envia@gmail.com
    BACKUP_SMTP_PASS=senha de app de 16 letras do Google
    BACKUP_SENHA=senha da criptografia, anotada também FORA do servidor
    BACKUP_DIAS=30                              (opcional)

Opcionais, que o gerar_backup.py da fozesc passa:

    BACKUP_RELATORIO=caminho de um PDF já protegido por senha, anexado à parte
    BACKUP_ASSUNTO=o que vai no fim do assunto (ex.: "relatório de outubro/2026")

Para não lotar a conta que envia, os e-mails do backup com mais de BACKUP_DIAS
dias vão para a lixeira dela, menos o último de cada mês (na fozesc, o do
relatório do mês), que fica para sempre. Se BACKUP_EMAIL for a mesma conta,
a limpeza vale para tudo.

Para abrir o anexo no Mac (pede a BACKUP_SENHA):
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -in ARQUIVO.enc | tar xz
"""

import email
import gzip
import imaplib
import io
import os
import re
import smtplib
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage

MAX_HORAS = 26
MAX_ANEXO = 20 * 1024 * 1024
CIFRA = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256"]


def ler_env(caminho):
    config = {}
    with open(caminho) as f:
        for linha in f:
            chave, sep, valor = linha.strip().partition("=")
            if sep and not chave.startswith("#"):
                config[chave.strip()] = valor.strip().strip("'\"")
    return config


def mais_recente(pasta, sufixos):
    achados = [
        os.path.join(raiz, nome)
        for raiz, _, nomes in os.walk(pasta)
        for nome in nomes
        if nome.endswith(sufixos)
    ]
    return max(achados, key=os.path.getmtime, default=None)


def dump_completo(caminho):
    """O pg_dump só escreve o rodapé quando chega ao fim."""
    try:
        with gzip.open(caminho, "rt", errors="replace") as f:
            return any("dump complete" in linha for linha in f)
    except (OSError, EOFError):
        return False


def escolher(pasta, agora):
    """Devolve os arquivos para mandar e a lista de problemas encontrados."""
    if not os.path.isdir(pasta):
        return [], [f"a pasta {pasta} não existe"]
    arquivos, problemas = [], []
    sql = mais_recente(pasta, (".sql.gz",))
    planilha = mais_recente(pasta, (".xlsx", ".zip"))
    if sql is None:
        problemas.append("nenhum .sql.gz")
    else:
        horas = (agora - os.path.getmtime(sql)) / 3600
        if horas > MAX_HORAS:
            problemas.append(f"o último backup tem {horas:.0f} horas — parou de rodar?")
        if not dump_completo(sql):
            problemas.append(f"{os.path.basename(sql)} está incompleto")
        arquivos.append(sql)
    if planilha is None:
        problemas.append("nenhuma planilha")
    else:
        arquivos.append(planilha)
    return arquivos, problemas


def empacotar(arquivos):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for caminho in arquivos:
            tar.add(caminho, arcname=os.path.basename(caminho))
    return buffer.getvalue()


def cifrar(dados, senha):
    # A senha vai pelo ambiente: na linha de comando qualquer um veria no `ps`.
    return subprocess.run(
        ["openssl", "enc", *CIFRA, "-salt", "-pass", "env:BACKUP_SENHA"],
        input=dados,
        capture_output=True,
        check=True,
        env={**os.environ, "BACKUP_SENHA": senha},
    ).stdout


def para_apagar(mensagens, agora, dias):
    """De [(uid, data, assunto)], os antigos que podem sair: fica o último
    de cada mês (de preferência um OK) e tudo dos últimos `dias` dias."""
    antigos = [m for m in mensagens if m[1] < agora - timedelta(days=dias)]
    guardar = {}
    for m in sorted(antigos, key=lambda m: ("— OK" not in m[2], -m[1].timestamp())):
        guardar.setdefault((m[1].year, m[1].month), m[0])
    return [m[0] for m in antigos if m[0] not in guardar.values()]


def limpar_antigos(config, nome, dias):
    with imaplib.IMAP4_SSL("imap.gmail.com", timeout=60) as imap:
        imap.login(config["BACKUP_SMTP_USER"], config["BACKUP_SMTP_PASS"].replace(" ", ""))
        # O nome das pastas muda com o idioma da conta; a marca \All/\Trash não.
        pastas = {}
        for linha in imap.list()[1]:
            marcas, _, pasta = linha.decode().partition(') "/" ')
            for marca in ("\\All", "\\Trash"):
                if marca in marcas.lstrip("(").split():
                    pastas[marca] = pasta
        imap.select(pastas["\\All"])
        busca = f'"subject:(Backup {nome}) older_than:{dias}d"'
        uids = imap.uid("SEARCH", "X-GM-RAW", busca)[1][0].split()
        if not uids:
            return 0
        mensagens = []
        resposta = imap.uid("FETCH", b",".join(uids), "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT)])")[1]
        for item in resposta:
            if not isinstance(item, tuple):
                continue
            uid = re.search(rb"UID (\d+)", item[0]).group(1)
            data = datetime(*imaplib.Internaldate2tuple(item[0])[:6])
            assunto = str(make_header(decode_header(email.message_from_bytes(item[1])["Subject"] or "")))
            if re.match(rf"Backup {nome} \d\d/\d\d/\d{{4}} — ", assunto):
                mensagens.append((uid, data, assunto))
        apagar = para_apagar(mensagens, datetime.now(), dias)
        if apagar:
            imap.uid("MOVE", b",".join(apagar), pastas["\\Trash"])
        return len(apagar)


def main():
    if len(sys.argv) > 2:
        sys.exit(f"Uso: {sys.argv[0]} [/caminho/do/.env]   (sem o .env, lê do ambiente)")
    config = ler_env(sys.argv[1]) if len(sys.argv) == 2 else dict(os.environ)
    faltando = [k for k in ("BACKUP_NOME", "BACKUP_PASTA", "BACKUP_EMAIL", "BACKUP_SMTP_USER",
                            "BACKUP_SMTP_PASS", "BACKUP_SENHA") if not config.get(k)]
    if faltando:
        sys.exit(f"Faltam no .env: {', '.join(faltando)}")
    if len(config["BACKUP_SENHA"]) < 12:
        sys.exit("BACKUP_SENHA curta demais (mínimo 12 caracteres)")
    nome = config["BACKUP_NOME"]
    if not re.fullmatch(r"[\w-]+", nome):
        sys.exit("BACKUP_NOME: use só letras, números e hífen")
    dias = int(config.get("BACKUP_DIAS") or 30)

    arquivos, problemas = escolher(config["BACKUP_PASTA"], time.time())
    hoje = datetime.now().strftime("%Y-%m-%d")
    anexo = cifrar(empacotar(arquivos), config["BACKUP_SENHA"]) if arquivos else b""
    relatorio = config.get("BACKUP_RELATORIO")
    if relatorio and not os.path.isfile(relatorio):
        problemas.append("o relatório PDF não foi gerado")
        relatorio = None
    if relatorio:
        anexo_extra = os.path.getsize(relatorio)
        if len(anexo) + anexo_extra > MAX_ANEXO:
            problemas.append(f"o relatório ({anexo_extra // 2**20} MB) não coube no e-mail")
            relatorio = None
    if len(anexo) > MAX_ANEXO:
        problemas.append(f"anexo com {len(anexo) // 2**20} MB passa do limite do Gmail: hora de trocar por Google Drive")
        anexo = b""

    linhas = [f"{os.path.basename(c)} ({os.path.getsize(c) // 1024} KB, "
              f"{datetime.fromtimestamp(os.path.getmtime(c)):%d/%m %H:%M})" for c in arquivos]
    if relatorio:
        linhas.append("")
        linhas.append(f"Relatório: {os.path.basename(relatorio)} (anexo à parte; abre em qualquer leitor de PDF "
                      "com a mesma senha da criptografia)")
    corpo = "\n".join(
        (["PROBLEMAS:", *problemas, ""] if problemas else [])
        + ["Arquivos:", *linhas, "", "Para abrir o backup no Mac (pede a senha da criptografia):",
           f"  openssl enc -d {' '.join(CIFRA)} -in backup-{nome.lower()}-{hoje}.tar.gz.enc | tar xz"]
    )

    msg = EmailMessage()
    situacao = f"PROBLEMA ({len(problemas)})" if problemas else "OK"
    extra = f" · {config['BACKUP_ASSUNTO']}" if config.get("BACKUP_ASSUNTO") else ""
    msg["Subject"] = f"Backup {nome} {datetime.now():%d/%m/%Y} — {situacao}{extra}"
    msg["From"] = config["BACKUP_SMTP_USER"]
    msg["To"] = config["BACKUP_EMAIL"]
    msg.set_content(corpo)
    if relatorio:
        with open(relatorio, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(relatorio))
    if anexo:
        msg.add_attachment(anexo, maintype="application", subtype="octet-stream",
                           filename=f"backup-{nome.lower()}-{hoje}.tar.gz.enc")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        # O Google mostra a senha de app em blocos com espaço; ela vale sem eles.
        smtp.login(config["BACKUP_SMTP_USER"], config["BACKUP_SMTP_PASS"].replace(" ", ""))
        smtp.send_message(msg)
    print(f"{datetime.now():%Y-%m-%d %H:%M} enviado: {msg['Subject']}")

    # Depois do envio: se a limpeza falhar, o backup de hoje já foi.
    try:
        print(f"  {limpar_antigos(config, nome, dias)} e-mail(s) antigo(s) para a lixeira")
    except Exception as erro:  # noqa: BLE001
        print(f"  a limpeza dos antigos falhou: {erro}")


if __name__ == "__main__":
    main()
