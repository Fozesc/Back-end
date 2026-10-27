#!/usr/bin/env python3
"""Manda por e-mail, com senha, uma cópia do backup mais recente: a cópia do banco (só para
quem foi marcado na aba Backups), a planilha com uma aba por assunto e o relatório PDF.

É a cópia fora do servidor: se a máquina estragar, o backup está no Gmail. Começou como o
script do Smaug; na fozesc quem chama é o gerar_backup.py, depois do backup das 15h, uma vez
para cada grupo de destinatários, passando o que vai no e-mail pelo ambiente.

Fora do Docker, passe o .env do sistema:

    python3 enviar_backups.py /home/lucas/fozesc/.env

Variáveis:

    BACKUP_NOME=Fozesc
    BACKUP_PASTA=/app/backups
    BACKUP_EMAIL=para.quem.vai@gmail.com        (vários: separe por vírgula)
    BACKUP_SMTP_USER=conta.que.envia@gmail.com
    BACKUP_SMTP_PASS=senha de app de 16 letras do Google
    BACKUP_SENHA=senha dos anexos, anotada também FORA do servidor
    BACKUP_DIAS=30                              (opcional)
    BACKUP_ARQUIVOS=banco,planilha,relatorio    (opcional; sem ela vão banco e planilha)
    BACKUP_RELATORIO=caminho do PDF já protegido pela mesma senha
    BACKUP_RELATORIO_TITULO=Relatório de outubro/2026
    BACKUP_RELATORIO_PERIODO=01/10/2026 a 30/10/2026

Para não lotar a conta que envia, os e-mails do backup com mais de BACKUP_DIAS dias vão para
a lixeira dela, menos os do último dia de cada mês, que ficam para sempre.

Para abrir a cópia do banco no Mac (pede a BACKUP_SENHA):
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -in ARQUIVO.sql.gz.enc -out ARQUIVO.sql.gz
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
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage

from msoffcrypto.format.ooxml import OOXMLFile
from openpyxl import load_workbook

MAX_HORAS = 26
MAX_ANEXO = 18 * 1024 * 1024
TIPO_XLSX = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
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


def cifrar_planilha(caminho, senha):
    saida = io.BytesIO()
    with open(caminho, "rb") as f:
        OOXMLFile(f).encrypt(senha, saida)
    return saida.getvalue()


def juntar(itens):
    return itens[0] if len(itens) == 1 else ", ".join(itens[:-1]) + " e " + itens[-1]


def cifrar(dados, senha):
    # A senha vai pelo ambiente: na linha de comando qualquer um veria no `ps`.
    return subprocess.run(
        ["openssl", "enc", *CIFRA, "-salt", "-pass", "env:BACKUP_SENHA"],
        input=dados,
        capture_output=True,
        check=True,
        env={**os.environ, "BACKUP_SENHA": senha},
    ).stdout


def com_problema(assunto):
    return "ATENÇÃO" in assunto or "PROBLEMA" in assunto


def para_apagar(mensagens, agora, dias):
    """De [(uid, data, assunto)], os antigos que podem sair: ficam os do último dia de cada
    mês (de preferência um dia sem problema) e tudo dos últimos `dias` dias."""
    antigos = [m for m in mensagens if m[1] < agora - timedelta(days=dias)]
    guardar = {}
    for m in sorted(antigos, key=lambda m: (com_problema(m[2]), -m[1].timestamp())):
        guardar.setdefault((m[1].year, m[1].month), m[1].date())
    return [m[0] for m in antigos if guardar[(m[1].year, m[1].month)] != m[1].date()]


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
    senha = config["BACKUP_SENHA"]
    if len(senha) < 12:
        sys.exit("BACKUP_SENHA curta demais (mínimo 12 caracteres)")
    nome = config["BACKUP_NOME"]
    if not re.fullmatch(r"[\w-]+", nome):
        sys.exit("BACKUP_NOME: use só letras, números e hífen")
    dias = int(config.get("BACKUP_DIAS") or 30)
    pedidos = [a.strip() for a in (config.get("BACKUP_ARQUIVOS") or "banco,planilha").split(",")]
    pasta = config["BACKUP_PASTA"]
    hoje = datetime.now()
    problemas, anexos = [], []

    def recente(caminho, o_que):
        quando = datetime.fromtimestamp(os.path.getmtime(caminho))
        if (hoje - quando).total_seconds() / 3600 > MAX_HORAS:
            problemas.append(f"{o_que} mais recente é de {quando:%d/%m/%Y às %H:%M}: o backup automático pode ter parado.")
        return quando

    if "relatorio" in pedidos:
        caminho = config.get("BACKUP_RELATORIO")
        if caminho and os.path.isfile(caminho):
            titulo = config.get("BACKUP_RELATORIO_TITULO") or "Relatório"
            periodo = config.get("BACKUP_RELATORIO_PERIODO")
            with open(caminho, "rb") as f:
                anexos.append({
                    "arquivo": f"{nome} - {titulo.replace('/', '-')}.pdf", "dados": f.read(),
                    "tipo": ("application", "pdf"), "curto": titulo[0].lower() + titulo[1:],
                    "texto": [f"{titulo} (PDF)",
                              "   Resumo do movimento" + (f" de {periodo}" if periodo else "")
                              + ": caixa, borderôs, recebimentos, prorrogações, vales e o que está em aberto."]})
        else:
            problemas.append("O relatório não foi gerado.")

    if "planilha" in pedidos:
        planilha = mais_recente(pasta, (".xlsx",)) if os.path.isdir(pasta) else None
        if planilha:
            quando = recente(planilha, "A planilha")
            livro = load_workbook(planilha, read_only=True)
            abas = livro.sheetnames
            livro.close()
            anexos.append({
                "arquivo": f"{nome} - Planilha {quando:%d-%m-%Y}.xlsx", "dados": cifrar_planilha(planilha, senha),
                "tipo": TIPO_XLSX, "curto": "planilha",
                "texto": ["Planilha com todos os dados do sistema (Excel)",
                          f"   Tudo o que estava no sistema em {quando:%d/%m/%Y às %H:%M}, "
                          f"uma aba para cada assunto: {juntar(abas)}."]})
        else:
            problemas.append("A planilha não foi gerada.")

    banco = None
    if "banco" in pedidos:
        sql = mais_recente(pasta, (".sql.gz",)) if os.path.isdir(pasta) else None
        if sql:
            quando = recente(sql, "A cópia do banco")
            if not dump_completo(sql):
                problemas.append("A cópia do banco está incompleta.")
            banco = f"{nome.lower()}-banco-{quando:%Y-%m-%d}.sql.gz.enc"
            with open(sql, "rb") as f:
                anexos.append({
                    "arquivo": banco, "dados": cifrar(f.read(), senha),
                    "tipo": ("application", "octet-stream"), "curto": "cópia do banco",
                    "texto": ["Cópia de segurança do banco de dados",
                              f"   O sistema inteiro em {quando:%d/%m/%Y às %H:%M}. Serve para restaurar o sistema; não precisa abrir."]})
        else:
            problemas.append("Não encontrei a cópia do banco.")

    total = 0
    for anexo in list(anexos):
        if total + len(anexo["dados"]) > MAX_ANEXO:
            problemas.append(f"{anexo['texto'][0]} ficou de fora: passou do limite de tamanho do Gmail.")
            anexos.remove(anexo)
            banco = None if anexo["arquivo"] == banco else banco
        else:
            total += len(anexo["dados"])

    linhas = [f"Backup da {nome} de {hoje:%d/%m/%Y}.", ""]
    if problemas:
        linhas += ["ATENÇÃO:", *[f"  - {p}" for p in problemas], ""]
    for anexo in anexos:
        linhas += [*anexo["texto"], ""]
    if anexos:
        linhas.append("Os arquivos abrem com a senha do backup.")
    if banco:
        linhas += ["", "Para abrir a cópia do banco no Mac (pede a senha):",
                   f"  openssl enc -d {' '.join(CIFRA)} -in {banco} -out {banco[:-4]}"]
    linhas += ["", f"Mensagem automática do sistema {nome}."]

    conteudo = juntar([a["curto"] for a in anexos]) if anexos else "nenhum arquivo"
    msg = EmailMessage()
    msg["Subject"] = f"Backup {nome} {hoje:%d/%m/%Y} — {'ATENÇÃO: ' if problemas else ''}{conteudo[0].upper()}{conteudo[1:]}"
    msg["From"] = config["BACKUP_SMTP_USER"]
    msg["To"] = config["BACKUP_EMAIL"]
    msg.set_content("\n".join(linhas))
    for anexo in anexos:
        msg.add_attachment(anexo["dados"], maintype=anexo["tipo"][0], subtype=anexo["tipo"][1], filename=anexo["arquivo"])

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(config["BACKUP_SMTP_USER"], config["BACKUP_SMTP_PASS"].replace(" ", ""))
        smtp.send_message(msg)
    print(f"{datetime.now():%Y-%m-%d %H:%M} enviado: {msg['Subject']}")

    try:
        print(f"  {limpar_antigos(config, nome, dias)} e-mail(s) antigo(s) para a lixeira")
    except Exception as erro:  # noqa: BLE001
        print(f"  a limpeza dos antigos falhou: {erro}")


if __name__ == "__main__":
    main()
