"""
Vales: criar tira da conta (saida no caixa), dar baixa devolve (entrada).
Banco SEPARADO (fozesc_teste_vales), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_vales.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_vales'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)

# tem que ser ANTES de importar o app: o Config le DATABASE_URL do ambiente
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = None
_db = None


def recria_banco():
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    cur.execute(f'CREATE DATABASE {BANCO_TESTE}')
    cur.close()
    conn.close()


def apaga_banco():
    global _db, _app
    if _db is not None and _app is not None:
        with _app.app_context():
            _db.engine.dispose()
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    cur.close()
    conn.close()


def main():
    global _app, _db
    recria_banco()
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, CompanySettings, Transaction, User, Vale
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': f"Bearer {r.get_json()['token']}"}
        saldo = lambda: http.get('/api/transactions/balances', headers=cab).get_json()['bruto']

        assert http.get('/api/vales').status_code == 401
        assert http.post('/api/vales', json={'descricao': 'X', 'valor': 10}).status_code == 401

        for ruim in ({'valor': 10}, {'descricao': '   ', 'valor': 10}, {'pessoa': 'Joao', 'valor': 10}, {'descricao': 'Joao', 'valor': 0}, {'descricao': 'Joao', 'valor': 'abc'},
                     {'descricao': 'Joao', 'valor': 10, 'conta': 'PIX'}, {'descricao': 'Joao', 'valor': 10, 'data': 'xx'}):
            assert http.post('/api/vales', json=ruim, headers=cab).status_code == 400, ruim
        assert saldo()['dinheiro_total'] == 0, 'invalido nao mexe no caixa'

        r = http.post('/api/vales', json={'descricao': 'Joao', 'valor': 300, 'conta': 'Dinheiro',
                                          'data': '2026-10-01'}, headers=cab)
        assert r.status_code == 201, r.data
        vid = r.get_json()['id']
        http.post('/api/vales', json={'descricao': 'Maria', 'valor': 50.5, 'conta': 'BB'}, headers=cab)
        s = saldo()
        assert s['dinheiro_total'] == -300 and s['bb_total'] == -50.5, s

        lista = http.get('/api/vales?status=Aberto', headers=cab).get_json()
        assert lista['total'] == 2 and lista['em_aberto'] == 350.5, lista
        assert http.get('/api/vales?search=mar', headers=cab).get_json()['total'] == 1
        assert len(http.get('/api/vales?per_page=99999', headers=cab).get_json()['items']) <= 100

        url = f'/api/vales/{vid}/pagamentos'
        for ruim in ({'valor': 0}, {'valor': 'NaN'}, {'valor': 301}, {'valor': 50, 'conta': 'PIX'},
                     {'valor': 50, 'partes': [{'conta': 'BB', 'valor': 20}]},
                     {'valor': 50, 'partes': [{'conta': 'BB', 'valor': 'NaN'}, {'conta': 'Dinheiro', 'valor': 50}]}):
            assert http.post(url, json=ruim, headers=cab).status_code == 400, ruim
        assert saldo()['bb_total'] == -50.5, 'pagamento invalido nao mexe no caixa'

        r = http.post(url, json={'valor': 100, 'conta': 'Dinheiro', 'data': '2026-10-02'}, headers=cab)
        assert r.status_code == 200, r.data
        j = r.get_json()
        assert j['status'] == 'Aberto' and j['pago'] == 100 and j['saldo'] == 200, j
        r = http.post(url, json={'valor': 80, 'data': '2026-10-03', 'partes': [
            {'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 30}, {'conta': 'BB', 'forma': 'PIX', 'valor': 50}]}, headers=cab)
        assert r.status_code == 200 and r.get_json()['saldo'] == 120, r.data
        s = saldo()
        assert s['dinheiro_total'] == -170 and s['bb_total'] == -0.5, s
        assert http.get('/api/vales', headers=cab).get_json()['em_aberto'] == 170.5

        d = http.get(f'/api/vales/{vid}', headers=cab).get_json()
        assert [(p['data'], p['valor'], p['conta']) for p in d['pagamentos']] == [
            ('2026-10-02', 100, 'Dinheiro'), ('2026-10-03', 30, 'Dinheiro'), ('2026-10-03', 50, 'BB')], d
        assert all(f'vale #{vid}' in p['descricao'] for p in d['pagamentos'])
        assert 'Parte 2/2 · PIX' in d['pagamentos'][2]['descricao']
        assert http.get('/api/vales/9999', headers=cab).status_code == 404

        r = http.post(url, json={'conta': 'Caixa'}, headers=cab)
        assert r.status_code == 200 and r.get_json()['status'] == 'Pago' and r.get_json()['saldo'] == 0, r.data
        assert saldo()['caixa_total'] == 120
        assert http.post(url, json={}, headers=cab).status_code == 400, 'pagar vale quitado'
        assert http.post('/api/vales/9999/pagamentos', json={}, headers=cab).status_code == 404
        assert saldo()['caixa_total'] == 120, 'pagamento duplo nao duplica entrada'
        assert http.get('/api/vales', headers=cab).get_json()['em_aberto'] == 50.5

        ultimo = http.get(f'/api/vales/{vid}', headers=cab).get_json()['pagamentos'][-1]['id']
        r = http.delete(f'/api/transactions/{ultimo}', json={'senha': SENHA}, headers=cab)
        assert r.status_code == 200, r.data
        j = http.get(f'/api/vales/{vid}', headers=cab).get_json()
        assert j['status'] == 'Aberto' and j['saldo'] == 120, 'apagar pagamento no caixa reabre o vale'

        r = http.post(url, json={'valor': 20, 'conta': 'Dinheiro', 'observacao': '  descontado   do <b>salario</b> '}, headers=cab)
        assert r.status_code == 200, r.data
        desc = http.get(f'/api/vales/{vid}', headers=cab).get_json()['pagamentos'][-1]['descricao']
        assert desc.endswith('· descontado do <b>salario</b>'), desc
        with app.app_context():
            db.session.add(Vale(pessoa='Antigo', descricao='do sistema velho', valor=5, data=date(2026, 1, 2), conta='Dinheiro'))
            db.session.commit()
        r = http.get('/api/vales?search=antigo&status=', headers=cab).get_json()
        assert r['total'] == 1 and r['items'][0]['descricao'] == 'Antigo - do sistema velho', r
        assert 'pessoa' not in r['items'][0]
        assert http.get('/api/vales/pessoas', headers=cab).status_code == 404, 'visao por pessoa saiu'
        with app.app_context():
            assert Transaction.query.filter_by(category='Vale', type='saida').order_by(Transaction.id).first() \
                .description == f'Vale #{vid} - Joao'

        with app.app_context():
            assert Transaction.query.filter_by(category='Vale').count() == 6
            acoes = sorted(l.action for l in AuditLog.query.filter_by(target='Vale'))
            assert acoes == ['BAIXA', 'CREATE', 'CREATE', 'PAGAMENTO', 'PAGAMENTO', 'PAGAMENTO'], acoes
            assert Vale.query.get(vid).data_pagamento is None

        # --- editar e apagar mexem no caixa junto ------------------------------
        antes = saldo()
        pid = http.post('/api/vales', json={'descricao': 'Pedro', 'valor': 1000, 'conta': 'Dinheiro',
                                             'data': '2026-10-05'}, headers=cab).get_json()['id']
        http.post(f'/api/vales/{pid}/pagamentos', json={'valor': 300, 'conta': 'BB', 'data': '2026-10-06'}, headers=cab)
        s = saldo()
        assert s['dinheiro_total'] == antes['dinheiro_total'] - 1000 and s['bb_total'] == antes['bb_total'] + 300, s

        ed = f'/api/vales/{pid}'
        novo = {'descricao': 'Pedro - adiantamento', 'valor': 800, 'conta': 'Caixa', 'data': '2026-10-04'}
        assert http.put(ed, json=novo).status_code == 401
        assert http.put(ed, json=novo, headers=cab).status_code == 403, 'sem senha'
        assert http.put(ed, json={**novo, 'senha': 'errada'}, headers=cab).status_code == 403
        assert http.put('/api/vales/9999', json={**novo, 'senha': SENHA}, headers=cab).status_code == 404
        for ruim in ({'valor': 200}, {'valor': 0}, {'valor': 'NaN'}, {'descricao': ' '}, {'conta': 'PIX'}, {'data': 'xx'}):
            assert http.put(ed, json={**novo, **ruim, 'senha': SENHA}, headers=cab).status_code == 400, ruim
        assert saldo() == s, 'edicao barrada nao mexe no caixa'

        r = http.put(ed, json={**novo, 'senha': SENHA}, headers=cab)
        assert r.status_code == 200, r.data
        j = r.get_json()
        assert j['valor'] == 800 and j['saldo'] == 500 and j['descricao'] == 'Pedro - adiantamento' and j['saida_no_caixa'], j
        s2 = saldo()
        assert s2['dinheiro_total'] == antes['dinheiro_total'] and s2['caixa_total'] == antes['caixa_total'] - 800, s2
        with app.app_context():
            t = Transaction.query.filter_by(vale_id=pid, type='saida').one()
            assert (t.amount, t.origin, str(t.date), t.description) == \
                (800, 'Caixa', '2026-10-04', f'Vale #{pid} - Pedro - adiantamento'), t.description

        r = http.put(ed, json={**novo, 'valor': 300, 'senha': SENHA}, headers=cab)
        assert r.get_json()['status'] == 'Pago', 'valor igual ao ja pago quita o vale'
        with app.app_context():
            v = db.session.get(Vale, pid)
            assert str(v.data_pagamento) == '2026-10-06' and v.conta_pagamento == 'BB'
        r = http.put(ed, json={**novo, 'valor': 450, 'senha': SENHA}, headers=cab)
        assert r.get_json()['status'] == 'Aberto' and r.get_json()['saldo'] == 150, 'aumentar o valor reabre'

        assert http.delete(ed, json={}, headers=cab).status_code == 403
        assert http.delete(ed, json={'senha': 'errada'}, headers=cab).status_code == 403
        r = http.delete(ed, json={'senha': SENHA}, headers=cab)
        assert r.status_code == 200 and r.get_json() == {'linhas_apagadas': 2, 'saida_no_caixa': True}, r.data
        assert saldo() == antes, 'apagar o vale tira a saida e os pagamentos: caixa volta ao que era'
        assert http.get(ed, headers=cab).status_code == 404
        assert http.delete(ed, json={'senha': SENHA}, headers=cab).status_code == 404

        # apagar so a saida pelo Fluxo de Caixa nao reabre nem quita o vale
        xid = http.post('/api/vales', json={'descricao': 'X', 'valor': 50, 'conta': 'Dinheiro'}, headers=cab).get_json()['id']
        http.post(f'/api/vales/{xid}/pagamentos', json={'conta': 'Dinheiro'}, headers=cab)
        with app.app_context():
            saida_x = Transaction.query.filter_by(vale_id=xid, type='saida').one().id
        assert http.delete(f'/api/transactions/{saida_x}', json={'senha': SENHA}, headers=cab).status_code == 200
        assert http.get(f'/api/vales/{xid}', headers=cab).get_json()['status'] == 'Pago'
        r = http.delete(f'/api/vales/{xid}', json={'senha': SENHA}, headers=cab)
        assert r.get_json() == {'linhas_apagadas': 1, 'saida_no_caixa': False}, r.data
        assert saldo() == antes

        # vale do sistema velho: saida e baixa gravadas sem vale_id
        with app.app_context():
            velho = Vale(pessoa='Carlos', valor=70, data=date(2026, 2, 1), conta='Dinheiro', status='Pago',
                         data_pagamento=date(2026, 2, 10), conta_pagamento='BB')
            sem_baixa = Vale(pessoa='Duda', valor=40, data=date(2026, 2, 3), conta='Dinheiro', status='Pago',
                             data_pagamento=date(2026, 2, 4), conta_pagamento='Dinheiro')
            db.session.add_all([velho, sem_baixa,
                Transaction(date=date(2026, 2, 1), description='Vale - Carlos', amount=70, type='saida',
                            origin='Dinheiro', category='Vale'),
                Transaction(date=date(2026, 2, 10), description='Pagamento de vale - Carlos', amount=70,
                            type='entrada', origin='BB', category='Vale')])
            db.session.commit()
            velho_id, duda_id = velho.id, sem_baixa.id
        r = http.put(f'/api/vales/{velho_id}', json={'descricao': 'Carlos', 'valor': 90, 'conta': 'Dinheiro',
                                                      'data': '2026-02-01', 'senha': SENHA}, headers=cab)
        j = r.get_json()
        assert r.status_code == 200 and j['saida_no_caixa'] and j['status'] == 'Aberto' and j['saldo'] == 20, j
        assert saldo()['dinheiro_total'] == antes['dinheiro_total'] - 90, 'achou e corrigiu a saida antiga'
        r = http.delete(f'/api/vales/{velho_id}', json={'senha': SENHA}, headers=cab)
        assert r.get_json() == {'linhas_apagadas': 2, 'saida_no_caixa': True}, r.data
        assert saldo() == antes, 'saida e baixa antigas sairam do caixa'

        r = http.put(f'/api/vales/{duda_id}', json={'descricao': 'Duda', 'valor': 50, 'conta': 'Dinheiro',
                                                     'data': '2026-02-03', 'senha': SENHA}, headers=cab)
        assert r.status_code == 400, 'quitado antigo sem a baixa no caixa: nao muda o valor'
        r = http.put(f'/api/vales/{duda_id}', json={'descricao': 'Duda - antigo', 'valor': 40, 'conta': 'Dinheiro',
                                                     'data': '2026-02-03', 'senha': SENHA}, headers=cab)
        j = r.get_json()
        assert r.status_code == 200 and j['status'] == 'Pago' and j['saida_no_caixa'] is False, j
        assert saldo() == antes, 'sem a linha no caixa, editar nao inventa lancamento'

        with app.app_context():
            acoes = [l.action for l in AuditLog.query.filter_by(target='Vale')]
            assert acoes.count('NEGADO') == 4 and acoes.count('DELETE') == 3, acoes
            apagado = AuditLog.query.filter_by(target='Vale', action='DELETE').order_by(AuditLog.id).first()
            assert 'Pedro - adiantamento' in apagado.description and 'R$ 450.00' in apagado.description, apagado.description

        print("OK: vale cria saida, pagamento parcial e dividido entra no caixa ligado ao vale, "
              "quitacao, mini caixa, apagar pagamento reabre, editar/apagar com senha levando o caixa junto "
              "(inclusive vale antigo), validacoes, paginacao, auditoria.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
