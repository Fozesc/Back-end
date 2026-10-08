"""
Troca entre contas no caixa (ex: R$ 500 em dinheiro por R$ 500 no PIX).

Rode:  ./venv/bin/python test_troca.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_troca'
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
        from app.models.domain import AuditLog, CompanySettings, Transaction, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()

        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA),
                                role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': f"Bearer {r.get_json()['token']}"}

        def saldo():
            return http.get('/api/transactions/balances', headers=cab).get_json()['bruto']

        def troca(**kw):
            base = {'valor': 500, 'data': '2026-10-07', 'descricao': 'Joao - dinheiro por PIX',
                    'conta_entrada': 'Dinheiro', 'conta_saida': 'Banco do Brasil'}
            return http.post('/api/transactions/troca', headers=cab, json={**base, **kw})

        assert http.post('/api/transactions/troca', json={}).status_code == 401
        for ruim in ({'valor': 0}, {'valor': 'abc'}, {'conta_saida': 'Dinheiro'}, {'conta_entrada': 'BB'},
                     {'conta_entrada': 'Nubank'}, {'data': 'ontem'}):
            assert troca(**ruim).status_code == 400, ruim
        with app.app_context():
            assert Transaction.query.count() == 0, 'entrada invalida nao grava nada'

        r = troca()
        assert r.status_code == 201, r.data
        saida, entrada = r.get_json()
        assert saida['tipo'] == 'saida' and saida['origem'] == 'BB', 'grava o nome padrao da conta'
        assert entrada['tipo'] == 'entrada' and entrada['origem'] == 'Dinheiro'
        assert saida['troca_id'] == entrada['troca_id'] == saida['id']
        assert abs(saida['valor']) == entrada['valor'] == 500
        s = saldo()
        assert s['dinheiro_total'] == 500 and s['bb_total'] == -500, s
        assert s['dinheiro_total'] + s['bb_total'] + s['caixa_total'] == 0, 'total do caixa nao muda'

        lista = http.get('/api/transactions', headers=cab).get_json()['items']
        assert {i['troca_id'] for i in lista} == {saida['id']}, lista

        r = http.put(f"/api/transactions/{entrada['id']}", headers=cab,
                     json={'valor': 700, 'descricao': 'Joao corrigido', 'tipo': 'saida'})
        assert r.status_code == 200, r.data
        with app.app_context():
            e, sa = db.session.get(Transaction, entrada['id']), db.session.get(Transaction, saida['id'])
            assert e.type == 'entrada', 'troca nao muda de tipo'
            assert (e.amount, sa.amount) == (700, -700), (e.amount, sa.amount)
            assert sa.description == 'Joao corrigido'

        r = http.put(f"/api/transactions/{entrada['id']}", headers=cab, json={'origem': 'Banco do Brasil'})
        assert r.status_code == 400, 'as duas pontas na mesma conta'

        r = http.delete(f"/api/transactions/{entrada['id']}", headers=cab, json={'senha': SENHA})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert Transaction.query.count() == 0, 'apagar uma ponta apaga a troca inteira'
            d = AuditLog.query.filter_by(action='DELETE').one().description
            assert 'TROCA' in d, d
        assert saldo()['dinheiro_total'] == 0

        # ------------- nome da conta padronizado: no que entra agora e no que ja estava gravado
        with app.app_context():
            for origem in ('Caixa Econômica', 'sistema (banco do brasil)', None):
                db.session.add(Transaction(date=date(2026, 10, 7), description='x', amount=1, type='entrada', origin=origem))
            db.session.commit()
            assert sorted(t.origin for t in Transaction.query.filter_by(description='x')) == ['Caixa', 'Dinheiro', 'Sistema (BB)']
            from sqlalchemy import text
            for velha in ('Banco do Brasil', 'CEF', 'Sistema (Caixa Econômica)', 'dinheiro'):
                db.session.execute(text("INSERT INTO transactions (date, description, amount, type, origin) "
                                        "VALUES ('2026-10-07', 'antiga', 10, 'entrada', :o)"), {'o': velha})
            db.session.commit()
        antes = saldo()

        def reiniciar():                              # o boot padroniza o que ja estava gravado
            outro = create_app()
            with outro.app_context():
                db.engine.dispose()
        reiniciar()
        with app.app_context():
            assert sorted(t.origin for t in Transaction.query.filter_by(description='antiga')) == \
                ['BB', 'Caixa', 'Dinheiro', 'Sistema (Caixa)']
            assert 'Contas do caixa padronizadas' in AuditLog.query.order_by(AuditLog.id.desc()).first().description
        assert saldo() == antes, 'padronizar o nome nao muda saldo nenhum'
        reiniciar()
        with app.app_context():
            assert AuditLog.query.filter(AuditLog.description.like('Contas do caixa padronizadas%')).count() == 1, \
                'rodar de novo nao mexe em nada'

        print("OK: troca grava entrada + saida ligadas com o mesmo valor, total do caixa "
              "nao muda, valida entrada no backend, editar sincroniza a outra ponta e "
              "apagar remove as duas; conta com nome padrao (Dinheiro/BB/Caixa) no novo e no antigo, sem mexer em saldo.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
