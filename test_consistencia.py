"""
Revisao geral: o caixa bate com titulos, borderôs e vales em todos os caminhos.
  - Dashboard e relatorio de atraso: Aguardando vencido e' atrasado (igual a tela de Titulos);
  - "Emprestado por origem": pela conta de onde o dinheiro saiu, nao pelo banco do cheque;
  - totais de Entradas/Saidas do Fluxo de Caixa sao do filtro inteiro, nao da pagina;
  - lancamento a mao validado (NaN, zero, tipo) e sem vinculo vindo da tela;
  - linha de borderô/titulo/vale: valor e tipo nao mudam pelo caixa (descricao, data e conta sim);
  - editar a data do borderô ou do pagamento leva os lancamentos do caixa junto;
  - linha que sai do caixa (ou vale apagado) recalcula o total do borderô;
  - titulo manual, status e borderô validados; historico do mes sem linha informativa.
Banco SEPARADO (fozesc_teste_consistencia), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_consistencia.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_consistencia'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
D = lambda n: (HOJE + timedelta(days=n)).isoformat()
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
        from app.models.domain import Check, Client, CompanySettings, Operation, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Lucas')
            db.session.add(cli)
            db.session.commit()
            id_cli = cli.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        get = lambda url, **q: http.get(url, headers=cab, query_string=q).get_json()
        saldos = lambda: get('/api/transactions/balances')

        def bordero(cheques, conta='Dinheiro', comissao=0, data=None, **extra):
            return http.post('/api/operations', headers=cab, json={
                'client_id': id_cli, 'operation_date': data or HOJE.isoformat(), 'taxa_mensal': 4,
                'dias_compensacao': 0, 'account_source': conta, 'comissao': comissao, 'iof_enabled': False,
                'checks': [{'valor': v, 'vencimento': venc, 'banco': banco, 'num_doc': f'N{i}'}
                           for i, (v, venc, banco) in enumerate(cheques, 1)], **extra})

        # ------------------------------------------------ borderô: validacao
        for ruim in ({'taxa_mensal': -4}, {'taxa_mensal': 'abc'}, {'dias_compensacao': 99},
                     {'account_source': 'Nubank'}, {'iof_enabled': True, 'iof_base': -1}):
            r = http.post('/api/operations', headers=cab, json={
                'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'account_source': 'BB',
                'checks': [{'valor': 100, 'vencimento': D(30)}], **ruim})
            assert r.status_code == 400, (ruim, r.data)
        with app.app_context():
            assert Operation.query.count() == 0, 'borderô invalido nao grava nada'

        # cheque do Banco do Brasil, mas o dinheiro saiu do Dinheiro; um a vencer e um ja vencido
        r = bordero([(1000, D(30), 'Banco do Brasil'), (500, D(-10), 'Caixa')], data=D(-40))
        assert r.status_code == 201, r.data
        op1 = r.get_json()['id']
        from app.services.operation_service import calcular_linha
        saida_op1 = round(calcular_linha(1000, 70, 4)[2] + calcular_linha(500, 30, 4)[2], 2)

        # ------------------------------------------------ emprestado por origem
        na_rua = saldos()['na_rua']
        assert (na_rua['BRASIL'], na_rua['CAIXA'], na_rua['DINHEIRO']) == (0, 0, 1500), \
            f'emprestado pela conta do borderô, nao pelo banco do cheque: {na_rua}'

        # ------------------------------------------------ dashboard: vencido e' atraso
        k = get('/api/dashboard')['kpis']
        assert (k['carteira'], k['inadimplencia']) == (1000, 500), k
        pizza = get('/api/dashboard')['charts']['pie_chart']
        assert (pizza[0], pizza[2]) == (1000, 500), pizza

        # ------------------------------------------------ lancamento a mao: validacao
        for ruim in ({'valor': 'NaN'}, {'valor': 'inf'}, {'valor': 0}, {'valor': 'x'}, {'valor': 10, 'tipo': 'roubo'},
                     {'valor': 10, 'data': '31/12/2026'}):
            r = http.post('/api/transactions', headers=cab, json={'descricao': 'teste', 'origem': 'BB', **ruim})
            assert r.status_code == 400, (ruim, r.data)
        r = http.post('/api/transactions', headers=cab, json={'descricao': 'aporte', 'valor': 300, 'tipo': 'entrada',
                                                              'origem': 'BB', 'operation_id': op1})
        assert r.status_code == 201 and r.get_json()['operation_id'] is None, 'vinculo nao vem da tela'
        id_aporte = r.get_json()['id']
        r = http.post('/api/transactions', headers=cab, json={'descricao': 'luz', 'valor': 50, 'tipo': 'saida', 'origem': 'BB'})
        assert r.get_json()['valor'] == -50, 'saida gravada negativa'
        assert saldos()['bruto']['bb_total'] == 250

        # ------------------------------------------------ totais do Fluxo: filtro inteiro
        lista = get('/api/transactions', per_page=1)
        assert lista['total'] == 3 and len(lista['items']) == 1, lista['total']
        assert (lista['summary']['entradas'], lista['summary']['saidas']) == (300, round(saida_op1 + 50, 2)), lista['summary']

        # ------------------------------------------------ editar linha vinculada pelo caixa
        with app.app_context():
            saida_op = Transaction.query.filter_by(operation_id=op1, category='Compra de Ativos').one()
            id_saida, valor_saida = saida_op.id, abs(saida_op.amount)
        r = http.put(f'/api/transactions/{id_saida}', headers=cab, json={'valor': 10})
        assert r.status_code == 400 and 'tela de origem' in r.get_json()['error'], r.data
        r = http.put(f'/api/transactions/{id_saida}', headers=cab, json={'tipo': 'entrada'})
        assert r.status_code == 400, r.data
        r = http.put(f'/api/transactions/{id_saida}', headers=cab,
                     json={'descricao': 'Pgto Borderô corrigido', 'valor': -valor_saida, 'tipo': 'saida', 'origem': 'Caixa'})
        assert r.status_code == 200 and r.get_json()['origem'] == 'Caixa', 'descricao/conta mudam'
        assert http.put(f'/api/transactions/{id_aporte}', headers=cab, json={'valor': 'NaN'}).status_code == 400
        assert http.put(f'/api/transactions/{id_aporte}', headers=cab, json={'valor': 400, 'tipo': 'entrada'}).status_code == 200
        assert saldos()['bruto']['bb_total'] == 350, 'lancamento a mao muda o valor normalmente'

        # ------------------------------------------------ editar a data do borderô / do pagamento
        with app.app_context():
            id_c1, id_c2 = [c.id for c in Check.query.filter_by(operation_id=op1).order_by(Check.id)]
        r = http.put(f'/api/checks/{id_c1}', headers=cab, json={'data_operacao': D(-39), 'senha': SENHA})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert db.session.get(Transaction, id_saida).date == HOJE - timedelta(days=39), 'a saida do borderô foi junto'
        assert http.patch(f'/api/checks/{id_c2}/status', headers=cab,
                          json={'status': 'Pago', 'payment_data': {'method': 'BB'}}).status_code == 200
        r = http.put(f'/api/checks/{id_c2}', headers=cab, json={'data_pagamento': D(-1), 'senha': SENHA})
        assert r.status_code == 200, r.data
        with app.app_context():
            rec = Transaction.query.filter_by(check_id=id_c2, category='Recebimento de Cheque').one()
            assert rec.date == HOJE - timedelta(days=1), 'o recebimento foi para a data nova'

        # ------------------------------------------------ status validado
        r = http.patch(f'/api/checks/{id_c1}/status', headers=cab, json={'status': 'Sumiu'})
        assert r.status_code == 400, r.data

        # ------------------------------------------------ titulo manual
        for ruim in ({'valor': 0}, {'valor': 100}, {'valor': 100, 'vencimento': '2026-02-30'},
                     {'valor': 100, 'vencimento': D(30), 'contaSaida': 'Nubank'},
                     {'valor': 100, 'vencimento': D(30), 'client_id': 99999}):
            r = http.post('/api/checks', headers=cab, json={'client_id': id_cli, **ruim})
            assert r.status_code == 400 and 'Traceback' not in r.get_json()['error'], (ruim, r.data)
        r = http.post('/api/checks', headers=cab, json={'client_id': id_cli, 'valor': 200, 'vencimento': D(20),
                                                        'contaSaida': 'Caixa', 'num_doc': 'M1'})
        assert r.status_code == 201, r.data
        assert saldos()['bruto']['caixa_total'] == round(-200 - saida_op1, 2), 'emprestimo manual saiu da Caixa'

        # ------------------------------------------------ total do borderô acompanha a linha que sai
        r = bordero([(2000, D(60), '')], conta='BB', comissao=2)
        assert r.status_code == 201, r.data
        op2 = r.get_json()['id']
        with app.app_context():
            ls = {t.category: t for t in Transaction.query.filter_by(operation_id=op2)}
            total, cliente, com = ls['Informativo'].id, abs(ls['Compra de Ativos'].amount), abs(ls['Comissão'].amount)
            id_com = ls['Comissão'].id
            assert db.session.get(Transaction, total).valor_informativo == round(cliente + com, 2)
        assert http.delete(f'/api/transactions/{id_com}', headers=cab, json={'senha': SENHA}).status_code == 200
        with app.app_context():
            assert db.session.get(Transaction, total).valor_informativo == round(cliente, 2), 'total sem a comissao apagada'

        # vale que recebeu desconto de comissao e foi apagado: o total do borderô volta
        vid = http.post('/api/vales', headers=cab, json={'descricao': 'Vale X', 'valor': 5000, 'conta': 'BB'}).get_json()['id']
        r = bordero([(3000, D(30), '')], conta='BB', comissao=2, vale_id=vid)
        assert r.status_code == 201, r.data
        op3 = r.get_json()['id']
        with app.app_context():
            ls = {t.category: t for t in Transaction.query.filter_by(operation_id=op3)}
            total3, cliente3 = ls['Informativo'].id, abs(ls['Compra de Ativos'].amount)
            assert db.session.get(Transaction, total3).valor_informativo == round(cliente3, 2), 'comissao toda no vale'
        assert http.delete(f'/api/vales/{vid}', headers=cab, json={'senha': SENHA}).status_code == 200
        with app.app_context():
            com3 = abs(Transaction.query.filter_by(operation_id=op3, category='Comissão').one().amount)
            assert db.session.get(Transaction, total3).valor_informativo == round(cliente3 + com3, 2), \
                'sem o desconto, a comissao sai inteira do banco'

        # ------------------------------------------------ historico do mes sem linha informativa
        mes = get(f'/api/history/month/{HOJE.year}/{HOJE.month}')
        assert mes['lancamentos'] and all(l['valor'] != 0 for l in mes['lancamentos']), 'linha informativa fora'

        print("OK: Dashboard/relatorio com vencido em atraso, emprestado pela conta do borderô, totais do Fluxo do "
              "filtro inteiro, lancamento a mao validado e sem vinculo, linha vinculada so muda descricao/data/conta, "
              "datas do borderô e do pagamento levam o caixa junto, status/titulo manual/borderô validados, total "
              "do borderô acompanha linha apagada e vale apagado, historico sem linha informativa.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
