"""
Folhas de calculo: cada usuario ve e mexe so nas proprias folhas, e os @valores do
sistema (caixa, carteira, vales, taxas, clientes) batem com os dados.
Banco SEPARADO (fozesc_teste_folhas), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_folhas.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_folhas'
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
        from app.models.domain import Check, Client, CompanySettings, Operation, Transaction, User, Vale
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        hoje = date.today()
        with app.app_context():
            for email in ('ana@fozesc.com', 'bia@fozesc.com'):
                db.session.add(User(name=email[:3], email=email, password_hash=generate_password_hash(SENHA),
                                    role='Operador', active=True))
            db.session.add(CompanySettings(default_monthly_rate=4.5))
            maria, jose, jose2 = Client(name='Maria Júlia', credit_limit=5000), Client(name='José'), Client(name='Jose')
            db.session.add_all([maria, jose, jose2])
            db.session.flush()
            op = Operation(client_id=maria.id, operation_date=hoje)
            db.session.add(op)
            db.session.flush()
            db.session.add_all([
                Check(operation_id=op.id, amount=1000, due_date=hoje + timedelta(days=5)),
                Check(operation_id=op.id, amount=300, due_date=hoje - timedelta(days=5)),
                Check(operation_id=op.id, amount=50, due_date=hoje, status='Pago'),
                Check(operation_id=op.id, amount=9999, due_date=hoje, fora_do_calculo=True),
                Transaction(date=hoje, amount=700, type='entrada', origin='BB', description='a'),
                Transaction(date=hoje, amount=-200, type='saida', origin='Dinheiro', description='b'),
                Vale(descricao='x', valor=80, data=hoje, conta='Dinheiro'),
            ])
            db.session.commit()

        def entrar(email):
            r = http.post('/api/auth/login', json={'email': email, 'password': SENHA})
            return {'Authorization': 'Bearer ' + r.get_json()['token']}
        ana, bia = entrar('ana@fozesc.com'), entrar('bia@fozesc.com')

        assert http.get('/api/folhas').status_code == 401
        r = http.post('/api/folhas', headers=ana, json={'titulo': '  Juros   do mês '})
        assert r.status_code == 201, r.data
        folha = r.get_json()
        assert folha['titulo'] == 'Juros do mês' and folha['conteudo'] == ''
        assert http.put(f"/api/folhas/{folha['id']}", headers=ana, json={'conteudo': 'a = 1\nsoma'}).status_code == 200
        assert http.put(f"/api/folhas/{folha['id']}", headers=ana, json={'titulo': ' '}).status_code == 400
        assert http.get('/api/folhas', headers=ana).get_json() == [{**folha, 'conteudo': 'a = 1\nsoma'}]

        # a outra pessoa nao ve, nao edita e nao apaga a folha da Ana
        assert http.get('/api/folhas', headers=bia).get_json() == []
        assert http.put(f"/api/folhas/{folha['id']}", headers=bia, json={'conteudo': 'x'}).status_code == 404
        assert http.delete(f"/api/folhas/{folha['id']}", headers=bia).status_code == 404
        assert http.delete(f"/api/folhas/{folha['id']}", headers=ana).status_code == 204
        assert http.get('/api/folhas', headers=ana).get_json() == []

        refs = {r['key']: r['value'] for r in http.get('/api/folhas/refs', headers=bia).get_json()}
        assert (refs['caixa'], refs['caixa.bb'], refs['caixa.dinheiro'], refs['caixa.caixa']) == (500, 700, -200, 0), refs
        assert (refs['carteira'], refs['carteira.a_vencer'], refs['carteira.atrasado']) == (1300, 1000, 300), refs
        assert refs['vales'] == 80 and refs['taxa'] == 4.5
        assert (refs['cliente.maria_julia'], refs['cliente.maria_julia.disponivel']) == (1300, 3700), refs
        assert 'cliente.jose' in refs and 'cliente.jose_2' in refs, 'dois José viram jose e jose_2'
        print('OK: folhas de cálculo por usuário (criar, salvar, apagar, ninguém mexe na do outro) '
              'e @valores do sistema (caixa, carteira sem o histórico, vales, taxa, clientes).')
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
