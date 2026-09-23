"""
Confere a exclusao de lancamento do caixa com SENHA e os filtros da Auditoria.

Apagar uma linha do caixa muda o saldo do banco na hora e nao tem desfazer, entao
passou a exigir a senha de quem esta logado - o mesmo 2o fator da edicao de cheque
e da mesclagem de cliente. A auditoria guarda o retrato completo da linha apagada
(data, valor, tipo, conta, categoria e de onde ela veio), que e o unico caminho de volta.

Roda em um banco SEPARADO (fozesc_teste_caixa), criado e apagado pelo proprio
teste - nao encosta nos dados reais.

Rode:  ./venv/bin/python test_caixa_exclusao.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_caixa'
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
        from app.models.domain import (AuditLog, Check, Client, CompanySettings,
                                        Operation, Transaction, User)
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()

        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA),
                                role='Admin', active=True))
            # get_balances estoura sem CompanySettings (bug pre-existente, ver README
            # do teste): em producao a linha existe, aqui criamos igual as outras telas
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Cliente Teste')
            db.session.add(cli)
            db.session.flush()

            op = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                           operation_date=date(2026, 9, 1), total_face_value=1000.0,
                           total_interest=100.0, total_net_value=900.0, status='Finalizada')
            db.session.add(op)
            db.session.flush()

            cheque = Check(operation_id=op.id, due_date=date(2026, 9, 20), amount=1000.0,
                           interest_amount=100.0, net_amount=900.0, status='Pago',
                           issuer_name='Emitente Teste', number='999')
            db.session.add(cheque)
            db.session.flush()

            solta = Transaction(date=date(2026, 9, 5), description='Gasto de escritorio',
                                amount=150.0, type='saida', origin='Dinheiro',
                                category='Despesa')
            do_cheque = Transaction(date=date(2026, 9, 20), description='Recebimento cheque 999',
                                    amount=1000.0, type='entrada', origin='BB',
                                    category='Cheque', check_id=cheque.id)
            do_bordero = Transaction(date=date(2026, 9, 1), description='Emprestimo bordero',
                                     amount=900.0, type='saida', origin='Caixa',
                                     category='Bordero', operation_id=op.id)
            db.session.add_all([solta, do_cheque, do_bordero])
            db.session.commit()
            id_solta, id_cheque, id_bordero = solta.id, do_cheque.id, do_bordero.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': f"Bearer {r.get_json()['token']}"}

        def saldo():
            return http.get('/api/transactions/balances', headers=cab).get_json()['bruto']

        def logs():
            with app.app_context():
                return [(l.action, l.target, l.description) for l in AuditLog.query.all()]

        def existe(tid):
            with app.app_context():
                return db.session.get(Transaction, tid) is not None

        antes = saldo()
        assert round(antes['bb_total'], 2) == 1000.0, antes
        assert round(antes['caixa_total'], 2) == -900.0, antes
        assert round(antes['dinheiro_total'], 2) == -150.0, antes

        # --- sem senha: 403 e nada apagado ------------------------------------
        r = http.delete(f'/api/transactions/{id_solta}', headers=cab, json={})
        assert r.status_code == 403, f"sem senha devia dar 403: {r.status_code} {r.data}"
        assert existe(id_solta), 'o lancamento NAO podia ter sido apagado'

        # --- senha errada: 403, nada apagado, e fica registrado ---------------
        r = http.delete(f'/api/transactions/{id_solta}', headers=cab, json={'senha': 'errada'})
        assert r.status_code == 403, r.status_code
        assert existe(id_solta), 'o lancamento NAO podia ter sido apagado'
        assert saldo() == antes, 'saldo nao podia mudar'
        negados = [l for l in logs() if l[0] == 'NEGADO']
        assert len(negados) == 2, f"as 2 tentativas deviam estar na auditoria: {negados}"
        assert all(l[1] == 'FluxoCaixa' for l in negados), negados

        # --- id que nao existe: 404 -------------------------------------------
        r = http.delete('/api/transactions/999999', headers=cab, json={'senha': SENHA})
        assert r.status_code == 404, r.status_code

        # --- sem login: 401 ---------------------------------------------------
        assert http.delete(f'/api/transactions/{id_solta}', json={'senha': SENHA}).status_code == 401

        # --- senha certa: apaga, mexe no saldo e grava o retrato completo -----
        r = http.delete(f'/api/transactions/{id_solta}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200, r.data
        assert not existe(id_solta), 'devia ter sido apagado'
        depois = saldo()
        assert round(depois['dinheiro_total'], 2) == 0.0, depois

        apagado = [l for l in logs() if l[0] == 'DELETE' and l[1] == 'FluxoCaixa']
        assert len(apagado) == 1, apagado
        d = apagado[0][2]
        for pedaco in ['Gasto de escritorio', '150.0', 'saida', 'Dinheiro', 'Despesa', '05/09/2026']:
            assert pedaco in d, f"o retrato precisa guardar {pedaco!r}: {d}"

        # --- linha vinculada a cheque: a auditoria registra o vinculo ----------
        r = http.delete(f'/api/transactions/{id_cheque}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200, r.data
        d = [l for l in logs() if l[0] == 'DELETE' and 'Recebimento cheque' in l[2]][0][2]
        assert 'VINCULADO A' in d and 'cheque #999' in d, d
        with app.app_context():
            assert db.session.get(Check, 1).status == 'Pago', 'o cheque nao e alterado'

        # --- linha vinculada a bordero ----------------------------------------
        r = http.delete(f'/api/transactions/{id_bordero}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200, r.data
        d = [l for l in logs() if l[0] == 'DELETE' and 'Emprestimo bordero' in l[2]][0][2]
        assert 'VINCULADO A' in d and 'borderô' in d, d

        # --- Auditoria: filtros vem do banco, com NEGADO junto ----------------
        f = http.get('/api/audit/filtros', headers=cab).get_json()
        assert 'NEGADO' in f['acoes'] and 'DELETE' in f['acoes'], f
        assert 'FluxoCaixa' in f['alvos'], f
        assert http.get('/api/audit/filtros').status_code == 401, 'filtros exigem login'

        # --- Auditoria: filtro por acao e por alvo ----------------------------
        r = http.get('/api/audit?action=NEGADO&per_page=50', headers=cab).get_json()
        assert r['total'] == 2, r['total']
        assert all(i['action'] == 'NEGADO' for i in r['items']), r['items']

        r = http.get('/api/audit?target=FluxoCaixa&per_page=50', headers=cab).get_json()
        assert all(i['target'] == 'FluxoCaixa' for i in r['items']), r['items']
        assert r['total'] == 5, f"2 negados + 3 exclusoes: {r['total']}"

        r = http.get('/api/audit?action=NEGADO&target=Cliente&per_page=50', headers=cab).get_json()
        assert r['total'] == 0, 'os dois filtros somam, nao substituem'

        print("OK: apagar do caixa exige senha (sem senha e senha errada barradas e "
              "registradas como NEGADO), saldo intacto quando barra, retrato completo "
              "na auditoria, vinculo com cheque/borderô registrado, cheque nao alterado, "
              "404/401 certos e filtros da Auditoria (acao + alvo) vindos do banco.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
