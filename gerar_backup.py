import os
import subprocess
import sys
import time
import json
import gzip
import calendar
import pandas as pd
import schedule
from datetime import date, datetime, timedelta
from dateutil.easter import easter
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

import relatorio_backup

DB_USER = os.getenv("POSTGRES_USER")
DB_PASS = os.getenv("POSTGRES_PASSWORD")
DB_NAME = os.getenv("POSTGRES_DB")
DB_HOST = os.getenv("POSTGRES_HOST", "db_fozesc")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")

if not DB_USER or not DB_PASS or not DB_NAME:
    raise ValueError("ERRO: Credenciais do banco não encontradas no ambiente. Backup abortado.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIR_DIARIO = os.path.join(BASE_DIR, "backups/diario")
DIR_SEMANAL = os.path.join(BASE_DIR, "backups/semanal")
DIR_MENSAL = os.path.join(BASE_DIR, "backups/mensal")

ARQUIVO_STATUS = os.path.join(BASE_DIR, "backups", "STATUS.json")
DIR_RELATORIOS = os.path.join(BASE_DIR, "backups/relatorios")
ARQUIVO_ULTIMO_EMAIL = os.path.join(BASE_DIR, "backups", "ULTIMO_EMAIL.json")
MIN_TABELAS = 5

def garantir_pastas():
    for pasta in [DIR_DIARIO, DIR_SEMANAL, DIR_MENSAL]:
        if not os.path.exists(pasta):
            os.makedirs(pasta)

def conferir_dump(caminho):
    """Abre o .gz e confirma que o pg_dump chegou ao fim. Sem isso, um dump
    truncado (disco cheio, container morto no meio) passa como valido."""
    tabelas = 0
    completo = False
    with gzip.open(caminho, "rt", errors="replace") as f:
        for linha in f:
            if linha.startswith("COPY "):
                tabelas += 1
            elif "PostgreSQL database dump complete" in linha:
                completo = True
    if not completo:
        return False, f"dump truncado: pg_dump nao chegou ao fim ({tabelas} tabelas)"
    if tabelas < MIN_TABELAS:
        return False, f"dump com apenas {tabelas} tabelas (esperado >= {MIN_TABELAS})"
    return True, f"{tabelas} tabelas, dump completo"

def gravar_status(ok, mensagem, arquivo=None):
    """Deixa o resultado do ultimo backup em disco. Foi a falta disso que
    escondeu 4 meses de .sql.gz vazios (mai-set/2026)."""
    garantir_pastas()
    with open(ARQUIVO_STATUS, "w") as f:
        json.dump({
            "ok": ok,
            "quando": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mensagem": mensagem,
            "arquivo": os.path.basename(arquivo) if arquivo else None,
        }, f, ensure_ascii=False, indent=2)

# Planilha do backup: uma aba por assunto, com o nome das colunas em portugues, para abrir no
# Excel sem restaurar o banco. De fora de proposito: usuarios (tem a senha), auditoria e tokens.
SITUACAO = "CASE WHEN c.status = 'Aguardando' AND c.due_date < CURRENT_DATE THEN 'Atrasado' ELSE c.status END"
ABAS = {
    "Títulos": f"""
        SELECT c.due_date AS "Vencimento", c.original_due_date AS "Vencimento original", cl.name AS "Cliente",
               c.issuer_name AS "Emitente", c.bank AS "Banco", c.number AS "Número", c.type AS "Tipo",
               COALESCE(c.original_amount, c.amount) AS "Valor de face", c.amount AS "Valor hoje",
               c.interest_amount AS "Juros", c.net_amount AS "Líquido", {SITUACAO} AS "Situação",
               c.payment_date AS "Pago em", c.paid_amount AS "Valor pago", c.payment_method AS "Forma de pagamento",
               o.id AS "Borderô", o.operation_date AS "Data do borderô",
               CASE WHEN c.fora_do_calculo THEN 'sim' ELSE '' END AS "Fora do cálculo", c.id AS "Id"
        FROM checks c JOIN operations o ON o.id = c.operation_id LEFT JOIN clients cl ON cl.id = o.client_id
        ORDER BY c.due_date, c.id""",
    "Clientes": """
        SELECT cl.name AS "Nome", cl.document AS "Documento", cl.phone AS "Telefone", cl.email AS "E-mail",
               cl.address AS "Endereço", cl.neighborhood AS "Bairro", cl.city AS "Cidade", cl.state AS "UF",
               cl.zip_code AS "CEP", cl.credit_limit AS "Limite", cl.standard_rate AS "Taxa (%)",
               COALESCE(ab.valor, 0) AS "Em aberto", COALESCE(ab.qtd, 0) AS "Títulos em aberto",
               cl.notes AS "Observação", cl.created_at AS "Cadastrado em", cl.id AS "Id"
        FROM clients cl LEFT JOIN (
            SELECT o.client_id, SUM(c.amount) AS valor, COUNT(*) AS qtd FROM checks c
            JOIN operations o ON o.id = c.operation_id
            WHERE c.status <> 'Pago' AND NOT c.fora_do_calculo GROUP BY o.client_id) ab ON ab.client_id = cl.id
        ORDER BY cl.name""",
    "Borderôs": """
        SELECT o.id AS "Borderô", o.operation_date AS "Data", COALESCE(cl.name, o.client_name_snapshot) AS "Cliente",
               (SELECT COUNT(*) FROM checks c WHERE c.operation_id = o.id) AS "Títulos",
               o.total_face_value AS "Valor de face", o.total_interest AS "Juros", o.iof_amount AS "IOF",
               o.comissao_valor AS "Comissão", o.total_net_value AS "Pago ao cliente", o.monthly_rate AS "Taxa (%)",
               o.account_source AS "Conta", o.status AS "Situação", o.notes AS "Observação"
        FROM operations o LEFT JOIN clients cl ON cl.id = o.client_id
        ORDER BY o.operation_date, o.id""",
    "Caixa": """
        SELECT t.date AS "Data", t.description AS "Descrição", t.category AS "Categoria", t.origin AS "Conta",
               CASE WHEN t.type = 'entrada' THEN ABS(t.amount) END AS "Entrada",
               CASE WHEN t.type <> 'entrada' THEN ABS(t.amount) END AS "Saída",
               t.operation_id AS "Borderô", t.check_id AS "Título (id)", t.vale_id AS "Vale", t.id AS "Id"
        FROM transactions t WHERE t.valor_informativo IS NULL
        ORDER BY t.date, t.id""",
    "Vales": """
        SELECT v.data AS "Data", CONCAT_WS(' - ', v.pessoa, v.descricao) AS "Descrição", v.conta AS "Saiu de",
               v.valor AS "Valor", CASE WHEN v.status = 'Pago' THEN v.valor ELSE COALESCE(p.pago, 0) END AS "Pago",
               CASE WHEN v.status = 'Pago' THEN 0 ELSE GREATEST(v.valor - COALESCE(p.pago, 0), 0) END AS "Falta pagar",
               v.status AS "Situação", v.data_pagamento AS "Quitado em", v.conta_pagamento AS "Conta da quitação",
               v.id AS "Vale"
        FROM vales v LEFT JOIN (SELECT vale_id, SUM(amount) AS pago FROM transactions
                                WHERE vale_id IS NOT NULL AND type = 'entrada' GROUP BY vale_id) p ON p.vale_id = v.id
        ORDER BY v.data, v.id""",
    "Prorrogações": """
        SELECT e.prorrogation_date AS "Data", cl.name AS "Cliente", c.issuer_name AS "Emitente", c.number AS "Número",
               e.old_due_date AS "Vencia", e.new_due_date AS "Novo vencimento", e.days_added AS "Dias",
               e.fee_amount AS "Juros", e.status AS "Situação", e.notes AS "Observação"
        FROM check_extensions e JOIN checks c ON c.id = e.check_id JOIN operations o ON o.id = c.operation_id
        LEFT JOIN clients cl ON cl.id = o.client_id
        ORDER BY e.prorrogation_date, e.id""",
    "Despesas recorrentes": """
        SELECT r.descricao AS "Descrição", CASE WHEN r.tipo = 'entrada' THEN 'Entrada' ELSE 'Saída' END AS "Tipo",
               r.valor AS "Valor", r.conta AS "Conta", r.categoria AS "Categoria", r.frequencia AS "Repete",
               COALESCE(r.adiado_ate, r.proximo) AS "Próximo vencimento",
               CASE WHEN r.automatico THEN 'Débito automático' ELSE 'Manual' END AS "Modo",
               CASE WHEN r.ativo THEN 'Ativa' ELSE 'Pausada' END AS "Situação", r.obs AS "Anotação"
        FROM recorrentes r ORDER BY COALESCE(r.adiado_ate, r.proximo), r.id""",
    "Calendário": """
        SELECT ev.data AS "Data", ev.titulo AS "Título", ev.tipo AS "Tipo", cl.name AS "Cliente", ev.valor AS "Valor",
               ev.previsao AS "Previsão", CASE WHEN ev.concluido THEN 'sim' ELSE '' END AS "Concluído",
               ev.descricao AS "Descrição", ev.autor AS "Autor"
        FROM eventos ev LEFT JOIN clients cl ON cl.id = ev.client_id
        ORDER BY ev.data, ev.id""",
    "Anotações de clientes": """
        SELECT n.criado_em AS "Data", cl.name AS "Cliente", n.autor AS "Autor", n.texto AS "Anotação"
        FROM client_notes n JOIN clients cl ON cl.id = n.client_id
        ORDER BY n.criado_em, n.id""",
}

def exportar_para_xlsx(caminho_pasta, prefixo):
    """Planilha com uma aba por assunto (ABAS). Aba que falha (tabela que ainda nao existe)
    fica de fora; as outras saem."""
    try:
        engine = create_engine(relatorio_backup.url_do_banco())
        xlsx_file = os.path.join(caminho_pasta, f"{prefixo}.xlsx")
        with pd.ExcelWriter(xlsx_file, engine='xlsxwriter', date_format='dd/mm/yyyy',
                            datetime_format='dd/mm/yyyy hh:mm') as writer:
            dinheiro = writer.book.add_format({'num_format': '#,##0.00'})
            for aba, sql in ABAS.items():
                try:
                    df = pd.read_sql(text(sql), engine)
                except Exception as e:
                    print(f"Aviso: a aba {aba} ficou de fora: {e}")
                    continue
                df.to_excel(writer, sheet_name=aba, index=False)
                folha = writer.sheets[aba]
                folha.freeze_panes(1, 0)
                folha.autofilter(0, 0, len(df), max(len(df.columns) - 1, 0))
                for i, coluna in enumerate(df.columns):
                    largura = max([len(str(coluna))] + [len(str(v)) for v in df[coluna].head(500)])
                    folha.set_column(i, i, min(max(largura + 2, 10), 50),
                                     dinheiro if df[coluna].dtype.kind == 'f' else None)
        engine.dispose()
        print(f"📊 Planilha gerada com sucesso: {os.path.basename(xlsx_file)}")
    except Exception as e:
        print(f"❌ Erro ao gerar XLSX: {e}")

def limpar_antigos(pasta, dias_limite):
    """ Apaga arquivos físicos modificados há mais de X dias """
    agora = datetime.now()
    limite_tempo = agora - timedelta(days=dias_limite)
    
    if not os.path.exists(pasta): return

    for filename in os.listdir(pasta):
        if filename.endswith(".gz") or filename.endswith(".xlsx"):
            filepath = os.path.join(pasta, filename)
            tempo_modificacao = datetime.fromtimestamp(os.path.getmtime(filepath))
            
            if tempo_modificacao < limite_tempo:
                os.remove(filepath)
                print(f"🧹 Limpeza executada: {filename} removido (mais de {dias_limite} dias de idade).")

def executar_backup(tipo="DIARIO"):
    garantir_pastas()
    agora = datetime.now()
    timestamp = agora.strftime("%Y-%m-%d_%H-%M-%S")
    
    if tipo == "MENSAL":
        pasta_destino = DIR_MENSAL
        prefixo = f"FOZESC_MENSAL_{timestamp}"
    elif tipo == "SEMANAL":
        pasta_destino = DIR_SEMANAL
        prefixo = f"FOZESC_SEMANAL_{timestamp}"
    else:
        pasta_destino = DIR_DIARIO
        prefixo = f"FOZESC_DIARIO_{timestamp}"
    
    sql_file = os.path.join(pasta_destino, f"{prefixo}.sql.gz")

    print(f"🐘 Iniciando extração de dados Postgres ({tipo})...")

    # CORREÇÃO 2: Removido o 'docker exec'. Agora usamos o cliente de rede interno apontando para o DB_HOST
    # CORREÇÃO 4: 'set -o pipefail' é obrigatório aqui. Sem ele, o código de saída
    # de "pg_dump | gzip" é o do GZIP (sucesso) mesmo quando o pg_dump falha ou nem
    # existe na imagem — o backup gravava um .sql.gz vazio de 20 bytes e mesmo assim
    # imprimia "salvo com sucesso". Agora a falha estoura de verdade.
    cmd = f"set -o pipefail; pg_dump -h {DB_HOST} -p {DB_PORT} -U {DB_USER} {DB_NAME} | gzip > {sql_file}"

    try:
        # CORREÇÃO 3: O pg_dump exige a senha injetada no ambiente para não travar aguardando digitação
        ambiente = os.environ.copy()
        ambiente["PGPASSWORD"] = DB_PASS

        subprocess.run(cmd, shell=True, check=True, env=ambiente,
                       executable="/bin/bash")  # pipefail é do bash, não do sh

        # CORREÇÃO 5: confere o resultado em vez de confiar no código de saída.
        tamanho = os.path.getsize(sql_file) if os.path.exists(sql_file) else 0
        if tamanho < 1024:
            raise RuntimeError(
                f"backup gerado com apenas {tamanho} bytes — arquivo vazio/inválido. "
                f"Verifique se o 'pg_dump' existe na imagem (pacote postgresql-client)."
            )
        # CORREÇÃO 6: tamanho não basta. Um dump truncado no meio tem tamanho
        # de sobra e continua inútil — só o rodapé do pg_dump prova que terminou.
        ok, detalhe = conferir_dump(sql_file)
        if not ok:
            raise RuntimeError(detalhe)

        print(f"✅ Arquivo SQL salvo em: {os.path.basename(sql_file)} ({tamanho/1024:.0f} KB, {detalhe})")

        exportar_para_xlsx(pasta_destino, prefixo)

        if tipo == "SEMANAL":
            print("Verificando se há backups DIÁRIOS com mais de 7 dias...")
            limpar_antigos(DIR_DIARIO, 7)
        elif tipo == "MENSAL":
            print("Verificando se há backups SEMANAIS com mais de 30 dias...")
            limpar_antigos(DIR_SEMANAL, 30)

        gravar_status(True, f"{tipo}: {detalhe}", sql_file)

    except Exception as e:
        print(f"❌ Falha crítica durante o backup: {e}")
        gravar_status(False, f"{tipo} FALHOU: {e}", sql_file)

def testar_restauracao(caminho=None):
    """Restaura o backup mais recente num banco descartável e confere os dados.
    Backup que nunca foi restaurado não é backup comprovado, é esperança."""
    if caminho is None:
        candidatos = []
        for pasta in [DIR_DIARIO, DIR_SEMANAL, DIR_MENSAL]:
            if os.path.isdir(pasta):
                candidatos += [os.path.join(pasta, f) for f in os.listdir(pasta)
                               if f.endswith(".sql.gz")]
        if not candidatos:
            print("❌ Nenhum backup .sql.gz encontrado para testar.")
            return False
        caminho = max(candidatos, key=os.path.getmtime)

    print(f"🧪 Testando restauração de: {os.path.basename(caminho)}")

    ok, detalhe = conferir_dump(caminho)
    if not ok:
        print(f"❌ Arquivo reprovado antes mesmo de restaurar: {detalhe}")
        return False
    print(f"   arquivo íntegro ({detalhe})")

    temp_db = "fozesc_teste_restauracao"
    if temp_db == DB_NAME:
        print("❌ Abortado: o banco de teste tem o mesmo nome do banco real.")
        return False

    ambiente = os.environ.copy()
    ambiente["PGPASSWORD"] = DB_PASS
    base = f"psql -h {DB_HOST} -p {DB_PORT} -U {DB_USER}"

    def rodar(cmd):
        return subprocess.run(cmd, shell=True, env=ambiente, executable="/bin/bash",
                              capture_output=True, text=True)

    rodar(f'{base} -d postgres -c "DROP DATABASE IF EXISTS {temp_db}"')
    r = rodar(f'{base} -d postgres -c "CREATE DATABASE {temp_db}"')
    if r.returncode != 0:
        print(f"❌ Não consegui criar o banco de teste: {r.stderr.strip()}")
        return False

    try:
        r = rodar(f"set -o pipefail; gunzip -c '{caminho}' | {base} -d {temp_db} -v ON_ERROR_STOP=1 -q")
        if r.returncode != 0:
            print(f"❌ A restauração falhou: {r.stderr.strip()[:400]}")
            return False

        consulta = ("select (select count(*) from clients) || ' clientes, ' || "
                    "(select count(*) from checks) || ' cheques, ' || "
                    "(select count(*) from operations) || ' borderos, ' || "
                    "(select count(*) from transactions) || ' transacoes'")
        r = rodar(f'{base} -d {temp_db} -tAc "{consulta}"')
        print(f"✅ Restauração comprovada — {r.stdout.strip()}")
        return True
    finally:
        rodar(f'{base} -d postgres -c "DROP DATABASE IF EXISTS {temp_db}"')

# ---------------------------------------------------------------- e-mail (copia fora do servidor)
# Cada arquivo (banco, planilha, relatorio) sai na frequencia escolhida na aba Backups, sempre
# depois do backup das 15h e so em dia util. Feriados de data fixa: os mesmos do
# Front-end/src/utils/diasUteis.js (24 e 31/12 entram: nao tem banco).
FERIADOS_FIXOS = {"01-01", "04-21", "05-01", "09-07", "10-12", "11-02", "11-15", "11-20", "12-24", "12-25", "12-31"}

def dia_util(d):
    if d.weekday() >= 5 or d.strftime("%m-%d") in FERIADOS_FIXOS:
        return False
    p = easter(d.year)  # Carnaval (segunda e terca), Sexta-feira Santa e Corpus Christi
    return d not in {p - timedelta(days=48), p - timedelta(days=47), p - timedelta(days=2), p + timedelta(days=60)}

def ultimo_dia_util(desde, ate):
    """O ultimo dia util entre `desde` e `ate` (`desde` se nenhum outro for)."""
    while ate > desde and not dia_util(ate):
        ate -= timedelta(days=1)
    return ate

def tipo_do_email(hoje):
    """'mensal' no ultimo dia util do mes, 'semanal' no ultimo dia util da semana (a sexta,
    ou antes se ela for feriado); nos outros dias, None."""
    if not dia_util(hoje):
        return None
    fim_do_mes = date(hoje.year, hoje.month, calendar.monthrange(hoje.year, hoje.month)[1])
    if ultimo_dia_util(hoje, fim_do_mes) == hoje:
        return "mensal"
    if ultimo_dia_util(hoje, hoje + timedelta(days=4 - hoje.weekday())) == hoje:
        return "semanal"
    return None

def ultimo_envio():
    try:
        with open(ARQUIVO_ULTIMO_EMAIL) as f:
            return date.fromisoformat(json.load(f)["data"])
    except (OSError, ValueError, KeyError):
        return None

def periodo_do_email(tipo, hoje, ultimo):
    """No ultimo dia util do mes: o mes inteiro. Nos outros: desde o dia do ultimo relatorio (ele
    entra de novo, para o que aconteceu depois das 15h daquele dia nao ficar de fora); no primeiro, 7 dias."""
    if tipo == "mensal":
        return hoje.replace(day=1), hoje
    return (ultimo if ultimo and ultimo <= hoje else hoje - timedelta(days=7)), hoje

ARQUIVOS = ("banco", "planilha", "relatorio")
FREQ_PADRAO = {"banco": "diario", "planilha": "semanal", "relatorio": "mensal"}

def ler_config():
    """A aba Backups das Configuracoes. Enquanto ela nao for salva, vale o .env do servidor."""
    engine = create_engine(relatorio_backup.url_do_banco())
    try:
        with engine.connect() as c:
            linha = c.execute(text("SELECT destinatarios, senha, freq_banco, freq_planilha, freq_relatorio "
                                   "FROM backup_config WHERE id = 1")).mappings().first()
    except ProgrammingError:
        linha = None
    finally:
        engine.dispose()
    if linha is None:
        emails = [e.strip() for e in os.getenv("BACKUP_EMAIL", "").split(",") if e.strip()]
        return {"destinatarios": [{"email": e, "banco": True} for e in emails],
                "senha": os.getenv("BACKUP_SENHA"), **FREQ_PADRAO}
    return {"destinatarios": linha["destinatarios"] or [], "senha": linha["senha"] or os.getenv("BACKUP_SENHA"),
            "banco": linha["freq_banco"], "planilha": linha["freq_planilha"], "relatorio": linha["freq_relatorio"]}

def arquivos_do_dia(cfg, hoje):
    """'semanal' sai no ultimo dia util da semana e tambem no do mes; 'mensal', so no do mes."""
    if not dia_util(hoje):
        return set()
    tipo = tipo_do_email(hoje)
    vale = {"diario": True, "semanal": tipo is not None, "mensal": tipo == "mensal"}
    return {a for a in ARQUIVOS if vale.get(cfg[a], False)}

def grupos(destinatarios, arquivos):
    """Quem recebe o banco num e-mail, quem nao recebe em outro. Grupo sem arquivo nao recebe nada."""
    saida = []
    for com_banco in (True, False):
        emails = [d["email"] for d in destinatarios if bool(d.get("banco")) == com_banco]
        lista = [a for a in ARQUIVOS if a in arquivos and (com_banco or a != "banco")]
        if emails and lista:
            saida.append((emails, lista))
    return saida

def gravar_ultimo_envio(ok, texto):
    engine = create_engine(relatorio_backup.url_do_banco())
    try:
        with engine.begin() as c:
            c.execute(text("UPDATE backup_config SET ultimo_envio = :j WHERE id = 1"),
                      {"j": json.dumps({"quando": datetime.now().strftime("%Y-%m-%d %H:%M"), "ok": ok, "texto": texto},
                                       ensure_ascii=False)})
    except Exception as e:
        print(f"Aviso: não gravei o resultado do envio no sistema: {e}")
    finally:
        engine.dispose()

def enviar_copia(hoje=None, arquivos=None):
    """Manda os arquivos do dia (ou `arquivos`, para testar): o banco so para quem foi marcado,
    a planilha e o relatorio PDF para todos, tudo com a senha do backup. O 'desde o ultimo
    relatorio' so anda se o relatorio chegou a todos."""
    hoje = hoje or date.today()
    try:
        cfg = ler_config()
    except Exception as e:
        print(f"❌ Não consegui ler a configuração do backup: {e}")
        return False
    if not cfg["destinatarios"]:
        print("📭 Cópia por e-mail desligada (ninguém na lista da aba Backups nem BACKUP_EMAIL no .env).")
        return False
    arquivos = arquivos_do_dia(cfg, hoje) if arquivos is None else set(arquivos)
    envios = grupos(cfg["destinatarios"], arquivos)
    if not envios:
        print("📭 Hoje não sai e-mail (pela frequência de cada arquivo na aba Backups).")
        return False

    pdf = titulo = periodo = None
    if "relatorio" in arquivos:
        inicio, fim = periodo_do_email(tipo_do_email(hoje), hoje, ultimo_envio())
        titulo = relatorio_backup.titulo_do_periodo(inicio, fim)
        periodo = f"{inicio:%d/%m/%Y} a {fim:%d/%m/%Y}"
        os.makedirs(DIR_RELATORIOS, exist_ok=True)
        pdf = os.path.join(DIR_RELATORIOS, f"relatorio-fozesc-{inicio}-a-{fim}.pdf")
        try:
            relatorio_backup.gerar(relatorio_backup.dados(inicio, fim), pdf, titulo, cfg["senha"])
            print(f"📄 Relatório gerado: {os.path.basename(pdf)}")
        except Exception as e:
            print(f"❌ O relatório PDF falhou (o resto vai assim mesmo): {e}")
            if os.path.exists(pdf):
                os.remove(pdf)
            pdf = None

    resultados = []
    for emails, lista in envios:
        ambiente = {**os.environ, "BACKUP_EMAIL": ",".join(emails), "BACKUP_SENHA": cfg["senha"] or "",
                    "BACKUP_ARQUIVOS": ",".join(lista), "BACKUP_RELATORIO": pdf or "",
                    "BACKUP_RELATORIO_TITULO": titulo or "", "BACKUP_RELATORIO_PERIODO": periodo or ""}
        try:
            r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "enviar_backups.py")],
                               env=ambiente, capture_output=True, text=True, timeout=600)
            ok, saida = r.returncode == 0, (r.stdout + r.stderr).strip() or f"saiu com o código {r.returncode}"
        except Exception as e:
            ok, saida = False, str(e)
        print(f"📧 {', '.join(emails)}: {saida}")
        resultados.append((emails, lista, ok, saida))

    nomes = {"banco": "banco", "planilha": "planilha", "relatorio": "relatório"}
    texto = "; ".join(f"{' e '.join(nomes[a] for a in lista)} para {', '.join(emails)}"
                      + ("" if ok else f" FALHOU: {saida.splitlines()[-1][:200]}")
                      for emails, lista, ok, saida in resultados)
    todos_ok = all(ok for *_, ok, _ in resultados)
    gravar_ultimo_envio(todos_ok, texto)
    if pdf and all(ok for _, lista, ok, _ in resultados if "relatorio" in lista):
        with open(ARQUIVO_ULTIMO_EMAIL, "w") as f:
            json.dump({"data": hoje.isoformat(), "periodo": periodo}, f)
    return todos_ok

def rotina_12h():
    print("⏰[12:00] Disparando backup diário da tarde...")
    executar_backup(tipo="DIARIO")

def rotina_15h():
    print("⏰[15:00] Disparando rotina de fechamento...")
    agora = datetime.now()
    ultimo_dia_do_mes = calendar.monthrange(agora.year, agora.month)[1]
    
    if agora.day == ultimo_dia_do_mes:
        executar_backup(tipo="MENSAL")
    elif agora.weekday() == 4: # Sexta-feira
        executar_backup(tipo="SEMANAL")
    else:
        executar_backup(tipo="DIARIO")
    enviar_copia()

if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else None

    if comando == "--testar":
        sys.exit(0 if testar_restauracao() else 1)

    if comando == "--email":
        sys.exit(0 if enviar_copia(arquivos=ARQUIVOS) else 1)

    if comando == "--agora":
        executar_backup(tipo="DIARIO")
        sys.exit(0)

    print("🕒 Módulo de Backups FOZESC iniciado com sucesso.")
    print("🕒 Aguardando os horários agendados (12h e 15h)...")

    schedule.every().day.at("12:00").do(rotina_12h)
    schedule.every().day.at("15:00").do(rotina_15h)

    while True:
        schedule.run_pending()
        time.sleep(60)