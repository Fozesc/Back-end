"""Confere que conferir_dump() reprova os backups quebrados que passaram batido
de maio a setembro/2026 (.sql.gz de 20 bytes dados como sucesso).

Rode: ./venv/bin/python test_backup.py
"""
import gzip
import os
import tempfile

os.environ.setdefault("POSTGRES_USER", "teste")
os.environ.setdefault("POSTGRES_PASSWORD", "teste")
os.environ.setdefault("POSTGRES_DB", "teste")

from gerar_backup import conferir_dump

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

    print("OK: dump vazio, truncado e incompleto reprovados; dump completo aprovado.")


if __name__ == "__main__":
    main()
