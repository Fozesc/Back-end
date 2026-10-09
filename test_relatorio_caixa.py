"""
Relatorio do caixa: geral e por conta (Dinheiro / BB / Caixa), com saldo anterior,
entradas, saidas, saldo final, categorias e extrato com o saldo linha a linha.
Banco SEPARADO (fozesc_teste_relatorio_caixa), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_relatorio_caixa.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_relatorio_caixa'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = _db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import CompanySettings, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))

            def t(dia, valor, tipo, conta, categoria, desc, **kw):
                db.session.add(Transaction(date=dia, amount=valor, type=tipo, origin=conta,
                                           category=categoria, description=desc, **kw))
            t(date(2026, 8, 31), 1000, 'entrada', 'Dinheiro', 'Aporte', 'aporte')          # antes do periodo
            t(date(2026, 8, 31), 500, 'entrada', 'BB', 'Aporte', 'aporte bb')
            t(date(2026, 9, 2), -300, 'saida', 'Sistema (Dinheiro)', 'Compra de Ativos', 'Pgto Borderô #1')
            t(date(2026, 9, 2), 0, 'saida', 'Sistema (Dinheiro)', 'Informativo', 'total informativo',
              valor_informativo=999)                                                         # nao mexe no saldo
            t(date(2026, 9, 5), 200, 'entrada', 'Dinheiro', 'Recebimento de Cheque', 'Recebimento Cheque #9')
            t(date(2026, 9, 6), 50, 'saida', 'BB', 'Vale', 'Vale #1 - Joao')                 # saida gravada positiva
            t(date(2026, 9, 8), 80, 'entrada', 'Caixa', 'Multas e Juros', 'Juros de prorrogação')
            t(date(2026, 9, 9), -100, 'saida', 'Dinheiro', 'Troca', 'troca')
            t(date(2026, 9, 9), 100, 'entrada', 'BB', 'Troca', 'troca')
            t(date(2026, 10, 5), 7, 'entrada', 'Dinheiro', 'Geral', 'depois do periodo')
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        URL = '/api/reports/caixa'
        setembro = {'inicio': '2026-09-01', 'fim': '2026-09-30'}

        def rel(**kw):
            r = http.get(URL, headers=cab, query_string={**setembro, **kw})
            assert r.status_code == 200, (kw, r.data)
            return r.get_json()

        assert http.get(URL, query_string=setembro).status_code == 401
        for ruim in ({'conta': 'nubank'}, {'tipo': 'x'}, {'inicio': '2026-10-01', 'fim': '2026-09-01'},
                     {'inicio': 'ontem'}):
            assert http.get(URL, headers=cab, query_string={**setembro, **ruim}).status_code == 400, ruim

        # ---------------------------------------------------------------- geral
        g = rel()
        assert g['resumo'] == {'saldo_anterior': 1500, 'entradas': 380, 'saidas': 450, 'qtd': 6,
                               'saldo_final': 1430}, g['resumo']
        por = {c['conta']: c for c in g['por_conta']}
        assert (por['Dinheiro']['saldo_anterior'], por['Dinheiro']['entradas'], por['Dinheiro']['saidas'],
                por['Dinheiro']['saldo_final']) == (1000, 200, 400, 800), por['Dinheiro']
        assert (por['BB']['entradas'], por['BB']['saidas'], por['BB']['saldo_final']) == (100, 50, 550), por['BB']
        assert (por['Caixa']['saldo_anterior'], por['Caixa']['saldo_final']) == (0, 80), por['Caixa']
        ext = g['extrato']
        assert ext['total'] == 6 and [i['descricao'] for i in ext['items']][:2] == ['Pgto Borderô #1', 'Recebimento Cheque #9']
        assert [i['saldo'] for i in ext['items']] == [1200, 1400, 1350, 1430, 1330, 1430], ext['items']
        assert ext['items'][0]['saida'] == 300 and ext['items'][0]['entrada'] is None, 'saida com valor positivo'
        assert 'total informativo' not in [i['descricao'] for i in ext['items']]
        assert g['empresa'] == 'Fozesc Teste'

        # ------------------------------------------------------------- uma conta
        d = rel(conta='dinheiro')
        assert d['resumo']['saldo_anterior'] == 1000 and d['resumo']['saldo_final'] == 800, d['resumo']
        assert [(i['descricao'], i['saldo']) for i in d['extrato']['items']] == [
            ('Pgto Borderô #1', 700), ('Recebimento Cheque #9', 900), ('troca', 800)], d['extrato']['items']
        assert {c['categoria']: c['total'] for c in d['categorias']['saida']} == {'Compra de Ativos': 300, 'Troca': 100}
        assert d['categorias']['entrada'] == [{'categoria': 'Recebimento de Cheque', 'total': 200, 'qtd': 1}]
        assert d['categorias']['saida'][0]['categoria'] == 'Compra de Ativos', 'maior primeiro'
        assert [i['conta'] for i in rel(conta='bb')['extrato']['items']] == ['BB', 'BB']
        assert rel(conta='caixa')['resumo']['saldo_final'] == 80

        # ------------------------------------- so entradas: o saldo continua o verdadeiro
        e = rel(tipo='entrada')
        assert [(i['descricao'], i['saldo']) for i in e['extrato']['items']] == [
            ('Recebimento Cheque #9', 1400), ('Juros de prorrogação', 1430), ('troca', 1430)], e['extrato']['items']
        assert e['resumo']['saidas'] == 450, 'o resumo nao depende do filtro de tipo'
        assert [i['saida'] for i in rel(tipo='saida')['extrato']['items']] == [300, 50, 100]

        # -------------------------------------------------------------- paginacao
        p2 = rel(per_page=2, page=2)['extrato']
        assert p2['pages'] == 3 and [i['saldo'] for i in p2['items']] == [1350, 1430], p2
        assert rel(per_page=99999)['extrato']['per_page'] == 1000, 'teto da pagina'

        # ------------------- ate hoje: bate com o saldo do Fluxo de Caixa conta por conta
        tudo = rel(inicio='2020-01-01', fim='2030-12-31')
        saldos = http.get('/api/transactions/balances', headers=cab).get_json()['bruto']
        por = {c['conta']: c['saldo_final'] for c in tudo['por_conta']}
        assert (por['Dinheiro'], por['BB'], por['Caixa']) == \
            (saldos['dinheiro_total'], saldos['bb_total'], saldos['caixa_total']), (por, saldos)

        print("OK: relatorio do caixa geral e por conta (saldo anterior, entradas, saidas, saldo final), "
              "categorias, extrato com saldo linha a linha (tambem filtrando so entradas/saidas), "
              "linha informativa fora, paginacao com teto, validacoes e bate com o saldo do Fluxo de Caixa.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
