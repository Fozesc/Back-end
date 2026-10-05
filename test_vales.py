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
        assert http.post('/api/vales', json={'pessoa': 'X', 'valor': 10}).status_code == 401

        for ruim in ({'valor': 10}, {'pessoa': 'Joao', 'valor': 0}, {'pessoa': 'Joao', 'valor': 'abc'},
                     {'pessoa': 'Joao', 'valor': 10, 'conta': 'PIX'}, {'pessoa': 'Joao', 'valor': 10, 'data': 'xx'}):
            assert http.post('/api/vales', json=ruim, headers=cab).status_code == 400, ruim
        assert saldo()['dinheiro_total'] == 0, 'invalido nao mexe no caixa'

        r = http.post('/api/vales', json={'pessoa': 'Joao', 'valor': 300, 'conta': 'Dinheiro',
                                          'data': '2026-10-01'}, headers=cab)
        assert r.status_code == 201, r.data
        vid = r.get_json()['id']
        http.post('/api/vales', json={'pessoa': 'Maria', 'valor': 50.5, 'conta': 'BB'}, headers=cab)
        s = saldo()
        assert s['dinheiro_total'] == -300 and s['bb_total'] == -50.5, s

        lista = http.get('/api/vales?status=Aberto', headers=cab).get_json()
        assert lista['total'] == 2 and lista['em_aberto'] == 350.5, lista
        assert http.get('/api/vales?search=mar', headers=cab).get_json()['total'] == 1
        assert len(http.get('/api/vales?per_page=99999', headers=cab).get_json()['items']) <= 100

        r = http.post(f'/api/vales/{vid}/baixa', json={'conta': 'Caixa'}, headers=cab)
        assert r.status_code == 200 and r.get_json()['status'] == 'Pago', r.data
        s = saldo()
        assert s['dinheiro_total'] == -300 and s['caixa_total'] == 300, s
        assert http.post(f'/api/vales/{vid}/baixa', json={}, headers=cab).status_code == 400, 'baixa dupla'
        assert http.post('/api/vales/9999/baixa', json={}, headers=cab).status_code == 404
        assert saldo()['caixa_total'] == 300, 'baixa dupla nao duplica entrada'
        assert http.get('/api/vales', headers=cab).get_json()['em_aberto'] == 50.5

        with app.app_context():
            assert Transaction.query.filter_by(category='Vale').count() == 3
            acoes = sorted(l.action for l in AuditLog.query.filter_by(target='Vale'))
            assert acoes == ['BAIXA', 'CREATE', 'CREATE'], acoes
            assert Vale.query.get(vid).data_pagamento is not None

        print("OK: vale cria saida no caixa, baixa cria entrada na conta escolhida, "
              "baixa dupla barrada, validacoes 400/401/404, paginacao limitada, auditoria.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
