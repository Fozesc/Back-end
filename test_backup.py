"""Confere que conferir_dump() reprova os backups quebrados que passaram batido
de maio a setembro/2026 (.sql.gz de 20 bytes dados como sucesso), e a regra do
e-mail: quando sai (ultimo dia util do mes e da semana), que periodo o relatorio
cobre e quais e-mails antigos a limpeza guarda.

Rode: ./venv/bin/python test_backup.py
"""
import gzip
import os
import tempfile

os.environ.setdefault("POSTGRES_USER", "teste")
os.environ.setdefault("POSTGRES_PASSWORD", "teste")
os.environ.setdefault("POSTGRES_DB", "teste")

from datetime import date, datetime

from enviar_backups import para_apagar
from gerar_backup import conferir_dump, dia_util, periodo_do_email, tipo_do_email

RODAPE = "--\n-- PostgreSQL database dump complete\n--\n"
TABELAS = ["clients", "checks", "operations", "transactions", "users",
           "company_settings", "audit_logs", "check_extensions", "token_blocklist"]


def gz(conteudo):
    caminho = os.path.join(tempfile.mkdtemp(), "d.sql.gz")
    with gzip.open(caminho, "wt") as f:
        f.write(conteudo)
    return caminho


def dump(tabelas, com_rodape=True):
    corpo = "".join(f"COPY public.{t} (id) FROM stdin;\n1\n\\.\n" for t in tabelas)
    return "-- PostgreSQL database dump\n" + corpo + (RODAPE if com_rodape else "")


def main():
    ok, msg = conferir_dump(gz(""))
    assert not ok, "dump VAZIO foi aprovado — é o bug de mai-set/2026"

    ok, msg = conferir_dump(gz(dump(TABELAS, com_rodape=False)))
    assert not ok, "dump TRUNCADO foi aprovado"
    assert "truncado" in msg, msg

    ok, msg = conferir_dump(gz(dump(TABELAS[:2])))
    assert not ok, "dump com poucas tabelas foi aprovado"

    ok, msg = conferir_dump(gz(dump(TABELAS)))
    assert ok, f"dump COMPLETO foi reprovado: {msg}"
    assert "9 tabelas" in msg, msg

    # ------------------------------------------------ quando o e-mail sai
    d = lambda s: date.fromisoformat(s)
    assert not dia_util(d("2026-04-03")) and not dia_util(d("2026-02-17")) and not dia_util(d("2026-06-04")), \
        "Sexta-feira Santa, Carnaval e Corpus Christi (pela Pascoa)"
    assert not dia_util(d("2026-11-20")) and dia_util(d("2026-11-19"))
    casos = {
        "2026-10-30": "mensal",    # sexta, 31/10 e' sabado
        "2026-10-29": None,
        "2026-10-23": "semanal",   # sexta comum
        "2026-10-24": None,        # sabado
        "2026-09-30": "mensal",    # quarta, ultimo dia util de setembro
        "2026-10-02": "semanal",   # sexta logo depois: o relatorio vem desde o dia 30
        "2026-04-02": "semanal",   # quinta: a sexta 03/04 e' Sexta-feira Santa
        "2026-04-03": None,
        "2026-12-30": "mensal",    # 31/12 sem banco: o mes (e a semana) fecham na quarta
        "2026-12-29": None,
        "2027-02-26": "mensal",    # 28/02/2027 e' domingo
    }
    for dia, tipo in casos.items():
        assert tipo_do_email(d(dia)) == tipo, (dia, tipo_do_email(d(dia)), tipo)

    # ------------------------------------------------ periodo do relatorio
    assert periodo_do_email("mensal", d("2026-10-30"), d("2026-10-23")) == (d("2026-10-01"), d("2026-10-30"))
    assert periodo_do_email("semanal", d("2026-10-23"), d("2026-10-16")) == (d("2026-10-16"), d("2026-10-23")), "desde o ultimo"
    assert periodo_do_email("semanal", d("2026-10-02"), d("2026-09-30")) == (d("2026-09-30"), d("2026-10-02"))
    assert periodo_do_email("semanal", d("2026-10-23"), None) == (d("2026-10-16"), d("2026-10-23")), "o primeiro: 7 dias"
    assert periodo_do_email("semanal", d("2026-10-23"), d("2027-01-01")) == (d("2026-10-16"), d("2026-10-23"))

    # ------------------------------------------------ limpeza: fica o ultimo OK de cada mes
    m = lambda uid, dia, fim: (uid, datetime.fromisoformat(dia + "T15:05"), f"Backup Fozesc {dia} — {fim}")
    caixa = [m(b"1", "2026-10-02", "OK · relatório de 25/09 a 02/10/2026"), m(b"2", "2026-10-23", "PROBLEMA (1)"),
             m(b"3", "2026-10-30", "OK · relatório de outubro/2026"), m(b"4", "2026-11-06", "OK"),
             m(b"5", "2026-11-13", "PROBLEMA (1)"), m(b"6", "2026-11-27", "OK"), m(b"7", "2026-12-04", "OK")]
    assert sorted(para_apagar(caixa, datetime(2026, 12, 15), 30)) == [b"1", b"2", b"5"], \
        "fica o relatorio de outubro (ultimo do mes) e o ultimo OK de novembro; os recentes nem entram"

    print("OK: dump vazio, truncado e incompleto reprovados; dump completo aprovado; e-mail no último dia útil "
          "do mês e da semana (feriados pela Páscoa), relatório do mês ou desde o último e-mail, e a limpeza guarda "
          "o último OK de cada mês.")


if __name__ == "__main__":
    main()
