"""
Saida do borderô dividida em contas (versao 2.11.2): o liquido sai de mais de uma conta, quanto de cada.

  - uma linha 'Compra de Ativos' por parte, cada uma na sua conta e ligadas (grupo), e o saldo de
    cada conta cai exatamente a parte dela; a conta do borderô e' a da maior parte;
  - soma que nao fecha com o liquido, conta ou valor invalido: recusado sem gravar nada;
  - com comissao: a linha de total, as partes do cliente e a comissao (da conta principal);
  - apagar um titulo tira o liquido dele das partes (da ultima para a primeira) e o total acompanha;
    apagar o ultimo leva o borderô inteiro;
  - o detalhe do titulo mostra "Dinheiro R$ ... + BB R$ ...";
  - uma parte so = borderô normal daquela conta.

Banco SEPARADO (fozesc_teste_dividido), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_bordero_dividido.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_dividido'
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
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Operation, Transaction, User
        from app.services.operation_service import arredondar_pagamento, calcular_comissao, calcular_linha
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Ana')
            db.session.add(cli)
            db.session.commit()
            id_cli = cli.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        saldos = lambda: http.get('/api/transactions/balances', headers=cab).get_json()['bruto']
        contas = lambda: (lambda s: (s['dinheiro_total'], s['bb_total'], s['caixa_total']))(saldos())

        CHEQUES = [(1000, 30), (2000, 60)]

        def linhas():
            ls = [list(calcular_linha(v, d, 4)) for v, d in CHEQUES]
            arredondar_pagamento(ls)
            return ls

        def bordero(partes, comissao=0, conta='Dinheiro'):
            return http.post('/api/operations', headers=cab, json={
                'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
                'account_source': conta, 'comissao': comissao, 'iof_enabled': False,
                **({'partes': partes} if partes is not None else {}),
                'checks': [{'valor': v, 'vencimento': D(d), 'num_doc': f'C{i}'} for i, (v, d) in enumerate(CHEQUES, 1)]})

        (j1, _, l1), (j2, _, l2) = linhas()
        liquido = round(l1 + l2, 2)
        assert liquido == int(liquido), 'o pagamento do cliente nao tem centavos'

        # ------------------------------------------------ invalido: nada gravado
        with app.app_context():
            antes = (Operation.query.count(), Check.query.count(), Transaction.query.count())
        for ruins, trecho in (([{'conta': 'Dinheiro', 'valor': 100}, {'conta': 'BB', 'valor': 100}], 'tem que fechar'),
                              ([{'conta': 'Itau', 'valor': liquido}], 'conta inválida'),
                              ([{'conta': 'Dinheiro', 'valor': liquido}, {'conta': 'BB', 'valor': 0}], 'maior que zero'),
                              ([{'conta': 'BB', 'forma': 'Bitcoin', 'valor': liquido}], 'forma')):
            r = bordero(ruins)
            assert r.status_code == 400 and trecho in r.get_json()['error'], (ruins, r.data)
        with app.app_context():
            assert (Operation.query.count(), Check.query.count(), Transaction.query.count()) == antes

        # ------------------------------------------------ dividido, sem comissao
        din, bb, cx = contas()
        r = bordero([{'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 1000},
                     {'conta': 'BB', 'forma': 'PIX', 'valor': round(liquido - 1000, 2)}])
        assert r.status_code == 201, r.data
        op1 = r.get_json()['id']
        assert contas() == (round(din - 1000, 2), round(bb - (liquido - 1000), 2), cx), 'cada conta cai a parte dela'
        with app.app_context():
            op = db.session.get(Operation, op1)
            assert op.account_source == 'BB', 'a conta do borderô e a da maior parte'
            ls = Transaction.query.filter_by(operation_id=op1).order_by(Transaction.id).all()
            assert [(t.category, t.origin, t.amount) for t in ls] == [
                ('Compra de Ativos', 'Sistema (Dinheiro)', -1000), ('Compra de Ativos', 'Sistema (BB)', -round(liquido - 1000, 2))]
            assert ls[0].description.endswith('(Parte 1/2 · Dinheiro)') and ls[1].description.endswith('(Parte 2/2 · PIX)')
            assert ls[0].grupo_id == ls[1].grupo_id == ls[0].id, 'as partes ficam juntas no caixa'
            ids = [c.id for c in Check.query.filter_by(operation_id=op1).order_by(Check.id)]
            assert 'Saiu de: Dinheiro R$ 1000.00 (Dinheiro) + BB R$' in \
                AuditLog.query.filter_by(target='Borderô').order_by(AuditLog.id.desc()).first().description
        det = http.get(f'/api/checks/{ids[0]}', headers=cab).get_json()['bordero']
        assert det['conta_saida'].startswith('Dinheiro R$ 1.000,00 + BB R$ '), det['conta_saida']

        # apagar o titulo 2: o liquido dele sai da ultima parte (BB) e o que faltar, da anterior
        r = http.delete(f'/api/checks/{ids[1]}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200, r.data
        with app.app_context():
            ls = Transaction.query.filter_by(operation_id=op1).order_by(Transaction.id).all()
            esperado_bb = round(max(liquido - 1000 - l2, 0), 2)
            esperado_din = round(1000 - max(l2 - (liquido - 1000), 0), 2)
            assert [abs(t.amount) for t in ls if t.amount] == [x for x in (esperado_din, esperado_bb) if x], \
                [(t.origin, t.amount) for t in ls]
            assert round(sum(abs(t.amount) for t in ls), 2) == round(liquido - l2, 2) == round(l1, 2)
        r = http.delete(f'/api/checks/{ids[0]}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['bordero_apagado'] is True, r.data
        assert contas() == (din, bb, cx), 'apagar tudo devolve cada conta'

        # ------------------------------------------------ dividido, com comissao
        com = calcular_comissao(round(j1 + j2, 2), 1, 4)
        din, bb, cx = contas()
        r = bordero([{'conta': 'Caixa', 'forma': 'TED/DOC', 'valor': round(liquido - 500, 2)},
                     {'conta': 'Dinheiro', 'valor': 500}], comissao=1)
        assert r.status_code == 201, r.data
        op2 = r.get_json()['id']
        with app.app_context():
            ls = Transaction.query.filter_by(operation_id=op2).order_by(Transaction.id).all()
            assert [(t.category, t.origin, t.amount, t.valor_informativo) for t in ls] == [
                ('Informativo', 'Sistema (Caixa)', 0, round(liquido + com, 2)),
                ('Compra de Ativos', 'Sistema (Caixa)', -round(liquido - 500, 2), None),
                ('Compra de Ativos', 'Sistema (Dinheiro)', -500, None),
                ('Comissão', 'Sistema (Caixa)', -com, None)], [(t.category, t.origin, t.amount, t.valor_informativo) for t in ls]
            assert len({t.grupo_id for t in ls}) == 1 and ls[1].description.startswith('Cliente recebe - Borderô #')
        assert contas() == (round(din - 500, 2), bb, round(cx - (liquido - 500) - com, 2))

        # ------------------------------------------------ uma parte so = borderô normal
        r = bordero([{'conta': 'BB', 'forma': 'PIX', 'valor': liquido}], conta='Dinheiro')
        assert r.status_code == 201, r.data
        with app.app_context():
            ls = Transaction.query.filter_by(operation_id=r.get_json()['id']).all()
            assert [(t.origin, t.amount, t.grupo_id) for t in ls] == [('Sistema (BB)', -liquido, None)]
            assert db.session.get(Operation, r.get_json()['id']).account_source == 'BB'
        r = bordero(None, conta='Caixa')
        assert r.status_code == 201
        with app.app_context():
            assert [t.origin for t in Transaction.query.filter_by(operation_id=r.get_json()['id'])] == ['Sistema (Caixa)'], \
                'sem partes continua como antes'

        print("OK: saida do borderô dividida em contas (uma linha por parte na sua conta, ligadas, saldo de cada conta "
              "certo, conta principal = maior parte), soma/conta/valor/forma invalidos recusados sem gravar, com "
              "comissao (total + partes + comissao da conta principal), apagar titulo tira das partes da ultima para a "
              "primeira, apagar tudo devolve cada conta, detalhe com as contas, e uma parte so = borderô normal.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
