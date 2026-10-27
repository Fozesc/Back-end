"""
Aba Backups das Configuracoes: so o Admin ve e muda, salvar pede a senha de login, os
e-mails sao conferidos no backend, a senha dos arquivos nunca volta para a tela nem para a
auditoria, e a auditoria diz o que mudou.

Roda em um banco SEPARADO (fozesc_teste_config_backup), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_config_backup.py
"""
import os
import re

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_config_backup'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA_ADMIN = 'Admin#Teste123'
SENHA_OPERADOR = 'Operador#Teste123'
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
        from app.models.domain import AuditLog, BackupConfig, User
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add_all([
                User(name='Lucas', email='lucas@fozesc.com', password_hash=generate_password_hash(SENHA_ADMIN), role='Admin'),
                User(name='Op', email='op@fozesc.com', password_hash=generate_password_hash(SENHA_OPERADOR), role='Operador'),
            ])
            db.session.commit()

        def entrar(email, senha):
            token = http.post('/api/auth/login', json={'email': email, 'password': senha}).get_json()['token']
            return {'Authorization': f'Bearer {token}'}

        admin, operador = entrar('lucas@fozesc.com', SENHA_ADMIN), entrar('op@fozesc.com', SENHA_OPERADOR)
        valido = {'senha_login': SENHA_ADMIN, 'freq_banco': 'diario', 'freq_planilha': 'semanal', 'freq_relatorio': 'mensal',
                  'destinatarios': [{'email': ' Lucas@Fozesc.com ', 'banco': True}, {'email': 'socio@gmail.com', 'banco': False}],
                  'senha_arquivos': 'senha-dos-arquivos-1'}
        put = lambda corpo, quem=admin: http.put('/api/settings/backup', json=corpo, headers=quem)

        r = http.get('/api/settings/backup', headers=admin)
        assert r.status_code == 200 and r.get_json()['configurado'] is False and r.get_json()['destinatarios'] == []
        assert http.get('/api/settings/backup', headers=operador).status_code == 403, 'operador nao ve'
        assert put(valido, operador).status_code == 403, 'operador nao muda'
        assert http.get('/api/settings/backup').status_code == 401

        r = put({**valido, 'senha_login': 'errada'})
        assert r.status_code == 403, r.get_json()
        with app.app_context():
            assert db.session.get(BackupConfig, 1) is None, 'senha errada: nada foi gravado'
            assert AuditLog.query.filter_by(action='NEGADO', target='Backup').count() == 1

        for ruim, motivo in (
            ({'destinatarios': [{'email': 'sem-arroba.com'}]}, 'e-mail sem @'),
            ({'destinatarios': [{'email': 'a@b.com, c@d.com'}]}, 'dois e-mails num campo'),
            ({'destinatarios': [{'email': 'a@b.com\nBcc: x@y.com'}]}, 'quebra de linha (cabecalho do e-mail)'),
            ({'destinatarios': [{'email': 'a@b.com'}, {'email': 'A@B.com'}]}, 'repetido'),
            ({'destinatarios': [{'email': f'a{i}@b.com'} for i in range(21)]}, 'mais de 20'),
            ({'destinatarios': 'a@b.com'}, 'lista que nao e lista'),
            ({'destinatarios': ['a@b.com']}, 'item que nao e objeto'),
            ({'freq_banco': 'toda hora'}, 'frequencia inventada'),
            ({'senha_arquivos': 'curta'}, 'senha curta'),
            ({'senha_arquivos': 'x' * 65}, 'senha longa'),
            ({'senha_arquivos': 123456789012345}, 'senha que nao e texto'),
        ):
            r = put({**valido, **ruim})
            assert r.status_code == 400, (motivo, r.status_code, r.get_json())

        r = put(valido)
        assert r.status_code == 200, r.get_json()
        cfg = r.get_json()
        assert cfg['destinatarios'] == [{'email': 'lucas@fozesc.com', 'banco': True}, {'email': 'socio@gmail.com', 'banco': False}]
        assert cfg['tem_senha'] is True and cfg['configurado'] is True
        assert 'senha' not in cfg and 'senha-dos-arquivos-1' not in r.get_data(as_text=True), 'a senha nao volta para a tela'
        with app.app_context():
            assert db.session.get(BackupConfig, 1).senha == 'senha-dos-arquivos-1'

        sem_trocar = {**valido, 'senha_arquivos': '', 'freq_planilha': 'mensal',
                      'destinatarios': [{'email': 'lucas@fozesc.com', 'banco': True}, {'email': 'socio@gmail.com', 'banco': True},
                                        {'email': 'novo@gmail.com', 'banco': False}]}
        assert put(sem_trocar).status_code == 200
        with app.app_context():
            assert db.session.get(BackupConfig, 1).senha == 'senha-dos-arquivos-1', 'senha em branco mantem a atual'
            logs = [a.description for a in AuditLog.query.filter_by(target='Backup', action='UPDATE').order_by(AuditLog.id)]
        assert len(logs) == 2, logs
        assert 'incluiu lucas@fozesc.com (com o banco)' in logs[0] and 'trocou a senha dos arquivos' in logs[0], logs[0]
        assert 'incluiu novo@gmail.com' in logs[1] and 'socio@gmail.com passou a receber o banco' in logs[1], logs[1]
        assert 'planilha: toda semana -> todo mês' in logs[1] and 'senha' not in logs[1], logs[1]
        assert not any('senha-dos-arquivos-1' in l for l in logs), 'a senha nao vai para a auditoria'

        with app.app_context():
            db.session.get(User, 2).role = 'Admin'
            db.session.commit()
        ex_admin = entrar('op@fozesc.com', SENHA_OPERADOR)
        with app.app_context():
            db.session.get(User, 2).role = 'Operador'
            db.session.commit()
        r = put({**valido, 'senha_login': SENHA_OPERADOR}, ex_admin)
        assert r.status_code == 403, 'deixou de ser Admin: o login antigo (que ainda diz Admin) nao muda nada'

        assert put({**sem_trocar, 'destinatarios': []}).status_code == 200, 'lista vazia desliga o e-mail'
        with app.app_context():
            assert 'retirou' in AuditLog.query.filter_by(target='Backup', action='UPDATE').order_by(AuditLog.id.desc()).first().description
        print('OK: aba Backups - só o Admin vê e muda (conferido também no banco, não só no login), salvar pede a senha de login, e-mails conferidos no backend '
              '(sem vírgula, quebra de linha ou repetido, até 20), a senha dos arquivos nunca volta para a tela nem '
              'para a auditoria, e a auditoria diz quem entrou, saiu e o que mudou.')
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
