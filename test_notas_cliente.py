"""
Notas da ficha do cliente: criar, listar (mais nova primeiro), editar, apagar.
Banco SEPARADO (fozesc_teste_notas), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_notas_cliente.py
"""
import os
import re

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_notas'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)

os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
_app = None
_db = None


def _admin(sql):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(sql)
    cur.close()
    conn.close()


def main():
    global _app, _db
    _admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    _admin(f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, Client, ClientNote, CompanySettings, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            for nome, email in (('Ana', 'ana@fozesc.com'), ('Bruno', 'bruno@fozesc.com')):
                db.session.add(User(name=nome, email=email, password_hash=generate_password_hash(SENHA),
                                    role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            db.session.add_all([Client(name='Cliente A'), Client(name='Cliente B')])
            db.session.commit()
            ca, cb = (c.id for c in Client.query.order_by(Client.id))

        def login(email):
            r = http.post('/api/auth/login', json={'email': email, 'password': SENHA})
            return {'Authorization': f"Bearer {r.get_json()['token']}"}
        ana, bruno = login('ana@fozesc.com'), login('bruno@fozesc.com')
        url = f'/api/clients/{ca}/notas'

        assert http.get(url).status_code == 401
        assert http.post(url, json={'texto': 'x'}).status_code == 401
        assert http.get('/api/clients/9999/notas', headers=ana).status_code == 404
        assert http.post('/api/clients/9999/notas', json={'texto': 'x'}, headers=ana).status_code == 404

        for ruim in ({}, {'texto': ''}, {'texto': '   '}, {'texto': 123}, {'texto': ['a']}, {'texto': 'a' * 4001}):
            assert http.post(url, json=ruim, headers=ana).status_code == 400, ruim
        assert http.post(url, data='nao e json', headers=ana).status_code == 400

        r = http.post(url, json={'texto': '  liga só de tarde\nprefere WhatsApp  '}, headers=ana)
        assert r.status_code == 201, r.data
        n1 = r.get_json()
        assert n1['autor'] == 'Ana' and n1['texto'] == 'liga só de tarde\nprefere WhatsApp', n1
        assert n1['criado_em'] and n1['editado_em'] is None, n1
        n2 = http.post(url, json={'texto': '<script>alert(1)</script>'}, headers=bruno).get_json()
        assert n2['autor'] == 'Bruno', 'autor e quem esta logado, nao o que vem no corpo'
        http.post(f'/api/clients/{cb}/notas', json={'texto': 'nota do B'}, headers=ana)

        lista = http.get(url, headers=ana).get_json()
        assert [n['id'] for n in lista['items']] == [n2['id'], n1['id']], 'mais nova primeiro'
        assert lista['total'] == 2 and lista['items'][0]['texto'] == '<script>alert(1)</script>'
        for _ in range(55):
            http.post(url, json={'texto': 'volume'}, headers=ana)
        assert len(http.get(url + '?per_page=99999', headers=ana).get_json()['items']) == 50, 'teto da pagina'
        assert http.get(url + '?page=2&per_page=50', headers=ana).get_json()['items'][-1]['id'] == n1['id']

        assert http.put(f"/api/clients/{cb}/notas/{n1['id']}", json={'texto': 'x'}, headers=ana).status_code == 404, \
            'nota de outro cliente nao se edita por este caminho'
        assert http.delete(f"/api/clients/{cb}/notas/{n1['id']}", headers=ana).status_code == 404
        assert http.put(f"{url}/{n1['id']}", json={'texto': ' '}, headers=ana).status_code == 400
        r = http.put(f"{url}/{n1['id']}", json={'texto': 'liga só de manhã'}, headers=bruno)
        assert r.status_code == 200, r.data
        e = r.get_json()
        assert e['texto'] == 'liga só de manhã' and e['editado_em'] and e['autor'] == 'Ana', e

        assert http.delete(f"{url}/{n2['id']}", headers=ana).status_code == 204
        assert http.delete(f"{url}/{n2['id']}", headers=ana).status_code == 404
        assert http.get(url, headers=ana).get_json()['total'] == 56

        r = http.post('/api/clients/merge', json={'origem_id': cb, 'destino_id': ca, 'senha': SENHA}, headers=ana)
        assert r.status_code == 200, r.data
        assert http.get(url, headers=ana).get_json()['total'] == 57, 'juntar cadastros leva as notas junto'

        with app.app_context():
            logs = {l.action: l.description for l in AuditLog.query.filter_by(target='Nota')}
            assert set(logs) == {'CREATE', 'UPDATE', 'DELETE'}, logs
            assert 'liga só de tarde' in logs['UPDATE'] and '<script>' in logs['DELETE'], logs
            assert ClientNote.query.filter_by(client_id=cb).count() == 0

        print("OK: notas do cliente com autor do login, texto validado no backend, mais nova primeiro, "
              "paginacao limitada, editar marca editado_em, nota de outro cliente barrada (404), apagar, "
              "auditoria com o texto antigo e juntar cadastros leva as notas.")
    finally:
        if _db is not None and _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        _admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
