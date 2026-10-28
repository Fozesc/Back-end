"""
Seguranca da versao 2.10:

1. Equipe (usuarios) e Auditoria so para Admin, conferido no BANCO (token antigo dizendo Admin
   nao vale). O ultimo Admin nao pode ser rebaixado nem apagado; ninguem apaga o proprio usuario.
   Cadastro valida nome, e-mail (sem repetir, sem diferenciar maiuscula), nivel e senha (8+).
2. Login: e-mail sem diferenciar maiuscula, usuario desativado nao entra, senha errada vira
   NEGADO na auditoria, e depois de 10 erros no minuto o login trava (429). Login certo nao conta.
   Usuario apagado/desativado perde o acesso na hora (o token dele para de valer). Login dura 12h.
3. Listas com teto de 100 por pagina (clientes, borderos, caixa, auditoria).
4. Backup na Auditoria: o worker grava BACKUP (deu certo) e FALHA (nao deu), alvo 'Backup'.

Banco SEPARADO (fozesc_teste_seguranca), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_seguranca.py
"""
import os
import re
import tempfile
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_seguranca'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')
os.environ.setdefault('POSTGRES_USER', 'teste')
os.environ.setdefault('POSTGRES_PASSWORD', 'teste')
os.environ.setdefault('POSTGRES_DB', 'teste')

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
        from app.models.domain import AuditLog, Client, CompanySettings, User
        from flask_jwt_extended import create_access_token
        from sqlalchemy import text
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            for nome, email, cargo in (('Chefe', 'chefe@fozesc.com', 'Admin'), ('Op', 'op@fozesc.com', 'Operador'),
                                       ('Saiu', 'saiu@fozesc.com', 'Operador')):
                db.session.add(User(name=nome, email=email, password_hash=generate_password_hash(SENHA), role=cargo, active=True))
            db.session.add_all([Client(name=f'Cliente {i:03}') for i in range(130)])
            db.session.add_all([AuditLog(user_name='x', action='CREATE', target='Teste', description=str(i)) for i in range(130)])
            db.session.commit()
            ids = {u.email: u.id for u in User.query}
            assert app.config['JWT_ACCESS_TOKEN_EXPIRES'] == timedelta(hours=12)
            assert db.session.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_audit_logs_timestamp'")).scalar()

        erros_login = 0

        def entrar(email, senha=SENHA):
            nonlocal erros_login
            r = http.post('/api/auth/login', json={'email': email, 'password': senha})
            erros_login += r.status_code == 401
            return r

        cab = lambda r: {'Authorization': 'Bearer ' + r.get_json()['token']}
        admin = cab(entrar('CHEFE@fozesc.com '))
        op = cab(entrar('op@fozesc.com'))
        saiu = cab(entrar('saiu@fozesc.com'))

        # ------------------------------------------------- equipe e auditoria: so Admin
        novo = {'name': 'Nova', 'email': 'nova@fozesc.com', 'password': 'senha-boa-123', 'role': 'Operador'}
        for metodo, url, corpo in (('get', '/api/users/', None), ('post', '/api/users/', novo),
                                   ('put', f"/api/users/{ids['op@fozesc.com']}", {**novo, 'role': 'Admin'}),
                                   ('delete', f"/api/users/{ids['saiu@fozesc.com']}", None),
                                   ('get', '/api/audit', None), ('get', '/api/audit/filtros', None)):
            r = getattr(http, metodo)(url, headers=op, json=corpo)
            assert r.status_code == 403, f'operador fez {metodo} {url}: {r.status_code}'
        with app.app_context():
            assert db.session.get(User, ids['op@fozesc.com']).role == 'Operador', 'operador nao se promoveu'
            falso = {'Authorization': 'Bearer ' + create_access_token(
                identity=str(ids['op@fozesc.com']), additional_claims={'role': 'Admin', 'name': 'Op'})}
        assert http.get('/api/users/', headers=falso).status_code == 403, 'token dizendo Admin nao basta'
        assert http.get('/api/users/', headers=admin).status_code == 200
        assert http.get('/api/audit/filtros', headers=admin).status_code == 200

        # validacao do cadastro
        U = '/api/users/'
        for ruim in ({**novo, 'password': '1234567'}, {**novo, 'role': 'Dono'}, {**novo, 'email': 'sem-arroba'},
                     {**novo, 'name': '  '}, {**novo, 'email': 'OP@fozesc.com'}, {}):
            assert http.post(U, headers=admin, json=ruim).status_code == 400, ruim
        r = http.post(U, headers=admin, json=novo)
        assert r.status_code == 201, r.data
        nova_id = r.get_json()['id']
        r = http.put(f'{U}{nova_id}', headers=admin, json={'name': 'Nova Silva', 'email': 'nova@fozesc.com', 'password': ''})
        assert r.status_code == 200 and r.get_json()['role'] == 'Operador', 'editar sem mandar senha/nivel mantem os dois'

        # senha que ja existe continua valendo (mesmo curta); so a NOVA precisa de 8+
        with app.app_context():
            curto = User(name='Curta', email='curta@fozesc.com', password_hash=generate_password_hash('12345'), role='Operador')
            db.session.add(curto)
            db.session.commit()
            curto_id = curto.id
        assert entrar('curta@fozesc.com', '12345').status_code == 200, 'senha antiga curta ainda entra'
        r = http.put(f'{U}{curto_id}', headers=admin, json={'name': 'Curta Silva', 'email': 'curta@fozesc.com', 'password': ''})
        assert r.status_code == 200 and entrar('curta@fozesc.com', '12345').status_code == 200, 'editar sem senha mantem a senha'
        assert http.put(f'{U}{curto_id}', headers=admin, json={'name': 'Curta', 'email': 'curta@fozesc.com',
                                                                'password': '1234567'}).status_code == 400, 'senha nova curta nao'
        assert entrar('curta@fozesc.com', '12345').status_code == 200

        # ultimo admin e o proprio usuario
        chefe = ids['chefe@fozesc.com']
        r = http.put(f'{U}{chefe}', headers=admin, json={'name': 'Chefe', 'email': 'chefe@fozesc.com', 'role': 'Operador'})
        assert r.status_code == 400 and 'único administrador' in r.get_json()['error'], r.data
        r = http.delete(f'{U}{chefe}', headers=admin)
        assert r.status_code == 400 and 'próprio' in r.get_json()['error'], r.data
        assert http.delete(f'{U}99999', headers=admin).status_code == 404

        # apagado perde o acesso na hora
        assert http.get('/api/clients', headers=saiu).status_code == 200
        assert http.delete(f"{U}{ids['saiu@fozesc.com']}", headers=admin).status_code == 200
        assert http.get('/api/clients', headers=saiu).status_code == 401, 'token de usuario apagado ainda valia'
        with app.app_context():
            db.session.get(User, nova_id).active = False
            db.session.commit()
        assert entrar('nova@fozesc.com', 'senha-boa-123').status_code == 401, 'desativado nao entra'
        with app.app_context():
            assert AuditLog.query.filter_by(action='NEGADO', target='Login').count() == 1
            retrato = AuditLog.query.filter_by(action='DELETE', target='Usuario').one().description
            assert 'saiu@fozesc.com' in retrato and 'Operador' in retrato, retrato

        # ------------------------------------------------------- teto da paginacao
        assert len(http.get('/api/clients?per_page=5000', headers=admin).get_json()['items']) == 100
        assert len(http.get('/api/clients?per_page=10', headers=admin).get_json()['items']) == 10
        assert len(http.get('/api/audit?per_page=5000', headers=admin).get_json()['items']) == 100
        assert http.get('/api/transactions?per_page=5000', headers=admin).status_code == 200
        assert http.get('/api/operations?per_page=5000', headers=admin).status_code == 200

        # ------------------------------------------- backup aparece na Auditoria
        import gerar_backup
        pasta = tempfile.mkdtemp()
        for nome in ('DIR_DIARIO', 'DIR_SEMANAL', 'DIR_MENSAL'):
            setattr(gerar_backup, nome, os.path.join(pasta, nome))
        gerar_backup.ARQUIVO_STATUS = os.path.join(pasta, 'STATUS.json')
        gerar_backup.DB_HOST = 'servidor-que-nao-existe.invalid'
        gerar_backup.auditar('BACKUP', 'Backup diário feito: teste')
        gerar_backup.executar_backup('DIARIO')          # sem banco para copiar: tem que virar FALHA
        bk = http.get('/api/audit?target=Backup', headers=admin).get_json()['items']
        assert [(b['action'], b['user']) for b in bk] == [('FALHA', 'Sistema (backup)'), ('BACKUP', 'Sistema (backup)')], bk
        assert bk[0]['details'].startswith('Backup diário FALHOU'), bk[0]
        assert not gerar_backup.dia_util(date(2026, 6, 10)), '10/06 (aniversario de Foz) nao e dia util'

        # aviso da tela inicial quando o backup falha ou para
        S = '/api/settings/backup/status'
        assert http.get(S, headers=op).status_code == 403
        st = http.get(S, headers=admin).get_json()
        assert [a['tipo'] for a in st['alertas']] == ['banco'] and 'FALHOU' in st['alertas'][0]['detalhe'], st
        gerar_backup.auditar('BACKUP', 'Backup diário feito: de novo')
        gerar_backup.auditar('FALHA', 'E-mail do backup: banco para x@x.com FALHOU: SMTP')
        st = http.get(S, headers=admin).get_json()
        assert [a['tipo'] for a in st['alertas']] == ['email'], 'a copia voltou a dar certo; o e-mail falhou'
        gerar_backup.auditar('BACKUP', 'E-mail do backup: banco para x@x.com')
        assert http.get(S, headers=admin).get_json()['alertas'] == [], 'tudo certo = sem aviso'
        with app.app_context():
            db.session.execute(text("UPDATE audit_logs SET timestamp = timestamp - interval '3 days' WHERE target = 'Backup'"))
            db.session.commit()
        st = http.get(S, headers=admin).get_json()
        assert [a['tipo'] for a in st['alertas']] == ['parado'], 'mais de 2 dias sem copia que deu certo'

        # --------------------------------------------- muitas senhas erradas travam
        assert entrar('chefe@fozesc.com').status_code == 200, 'login certo nao conta para a trava'
        while erros_login < 10:
            assert entrar('chefe@fozesc.com', 'errada').status_code == 401
        r = entrar('chefe@fozesc.com', 'errada')
        assert r.status_code == 429, f'11a senha errada no minuto devia travar: {r.status_code}'
        assert entrar('chefe@fozesc.com').status_code == 429, 'travado vale ate para a senha certa'

        print("OK: equipe e auditoria so para Admin (cargo do banco), cadastro validado, ultimo Admin e o proprio "
              "usuario protegidos, apagado/desativado perde o acesso, login sem maiuscula, senha errada na "
              "auditoria e trava apos 10 erros, login de 12h, teto de 100 por pagina, indice da auditoria, "
              "backup (BACKUP/FALHA) na auditoria, aviso de backup com falha ou parado e 10/06 como feriado.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
