"""
Confere a trava de "fora do calculo" e a edicao de cheque com senha.

Roda em um banco SEPARADO (fozesc_teste_calculo), criado e apagado pelo proprio
teste - nao encosta nos dados reais.

Rode:  ./venv/bin/python test_fora_do_calculo.py
"""
import os
import re

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_calculo'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)

# tem que ser ANTES de importar o app: o Config le DATABASE_URL do ambiente
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


def recria_banco():
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')
    cur.execute(f'CREATE DATABASE {BANCO_TESTE}')
    cur.close()
    conn.close()


def apaga_banco():
    # solta as conexoes do pool do SQLAlchemy, senao o DROP reclama que o banco
    # esta em uso e o banco de teste fica para tras
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


SENHA = 'Senha#Teste123'
_app = None
_db = None


def main():
    global _app, _db
    recria_banco()
    try:
        from app import create_app, db
        from app.models.domain import Check, Client, Operation, User
        from werkzeug.security import generate_password_hash

        app = create_app()          # create_all + o ALTER da coluna nova
        _app, _db = app, db
        cliente_http = app.test_client()

        with app.app_context():
            # a coluna nova precisa existir mesmo em banco criado do zero
            from sqlalchemy import inspect
            colunas = [c['name'] for c in inspect(db.engine).get_columns('checks')]
            assert 'fora_do_calculo' in colunas, f"coluna nao criada: {colunas}"

            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA),
                                role='Admin', active=True))
            cli = Client(name='Cliente Teste')
            db.session.add(cli)
            db.session.flush()

            op = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                           operation_date=__import__('datetime').date(2026, 9, 1),
                           total_face_value=3000.0, total_interest=300.0,
                           total_net_value=2700.0, status='Finalizada')
            db.session.add(op)
            db.session.flush()

            pago = Check(operation_id=op.id, due_date=__import__('datetime').date(2026, 9, 20),
                         amount=1000.0, interest_amount=100.0, net_amount=900.0,
                         status='Pago', issuer_name='Emitente Pago', number='111')
            aberto = Check(operation_id=op.id, due_date=__import__('datetime').date(2026, 12, 20),
                           amount=2000.0, interest_amount=200.0, net_amount=1800.0,
                           status='Aguardando', issuer_name='Emitente Aberto', number='222')
            juridico = Check(operation_id=op.id, due_date=__import__('datetime').date(2026, 10, 10),
                             amount=500.0, interest_amount=50.0, net_amount=450.0,
                             status='Juridico', issuer_name='Emitente Juridico', number='333')
            db.session.add_all([pago, aberto, juridico])
            db.session.commit()
            id_pago, id_aberto, id_juridico = pago.id, aberto.id, juridico.id

            from app.services.dashboard_service import DashboardService

            def kpis():
                return DashboardService().get_dashboard_data('meses')['kpis']

            # --- ponto de partida: os tres contam -----------------------------
            # lucro = juros de cheque PAGO que esta no calculo (so o de 100),
            # nao a soma dos tres (350): juros de cheque em aberto nao e lucro.
            k = kpis()
            assert round(k['lucro'], 2) == 100.0, k
            assert round(k['carteira'], 2) == 2000.0, k
            assert round(k['inadimplencia'], 2) == 500.0, k

        # --- login para ter token (a edicao exige usuario logado) -------------
        r = cliente_http.post('/api/auth/login',
                              json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        token = r.get_json()['token']
        cab = {'Authorization': f'Bearer {token}'}

        # --- tirar do calculo por ID -----------------------------------------
        r = cliente_http.patch('/api/checks/calculo', headers=cab,
                               json={'fora': True, 'ids': [id_pago]})
        assert r.status_code == 200 and r.get_json()['alterados'] == 1, r.data
        with app.app_context():
            k = kpis()
            assert round(k['lucro'], 2) == 0.0, f"juros do pago devia sair do lucro: {k}"
            assert round(k['carteira'], 2) == 2000.0, k

        # --- tirar do calculo pelo FILTRO (todos os do Juridico) -------------
        r = cliente_http.patch('/api/checks/calculo', headers=cab,
                               json={'fora': True, 'filtros': {'status': 'Juridico'}})
        assert r.status_code == 200 and r.get_json()['alterados'] == 1, r.data
        with app.app_context():
            k = kpis()
            assert round(k['inadimplencia'], 2) == 0.0, f"juridico devia sair: {k}"
            assert round(k['lucro'], 2) == 0.0, k

        # --- e voltar ---------------------------------------------------------
        r = cliente_http.patch('/api/checks/calculo', headers=cab,
                               json={'fora': False, 'filtros': {'status': 'Juridico'}})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert round(kpis()['inadimplencia'], 2) == 500.0

        # --- devolver o pago ao calculo traz o lucro de volta -----------------
        r = cliente_http.patch('/api/checks/calculo', headers=cab,
                               json={'fora': False, 'ids': [id_pago]})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert round(kpis()['lucro'], 2) == 100.0, kpis()

        # --- pedido sem 'fora' e recusado ------------------------------------
        assert cliente_http.patch('/api/checks/calculo', headers=cab, json={}).status_code == 400

        # --- edicao: sem senha e com senha errada NAO passam -----------------
        r = cliente_http.put(f'/api/checks/{id_aberto}', headers=cab,
                             json={'vencimento': '2027-01-10'})
        assert r.status_code == 403, f"sem senha devia dar 403: {r.status_code} {r.data}"

        r = cliente_http.put(f'/api/checks/{id_aberto}', headers=cab,
                             json={'vencimento': '2027-01-10', 'senha': 'errada'})
        assert r.status_code == 403, f"senha errada devia dar 403: {r.status_code}"

        with app.app_context():
            c = db.session.get(Check, id_aberto)
            assert str(c.due_date) == '2026-12-20', f"nao podia ter mudado: {c.due_date}"

        # --- ano absurdo e recusado (o bug 2005/1902 da planilha) ------------
        r = cliente_http.put(f'/api/checks/{id_aberto}', headers=cab,
                             json={'vencimento': '1902-09-18', 'senha': SENHA})
        assert r.status_code == 400, f"ano 1902 devia ser recusado: {r.status_code}"

        # --- com a senha certa: muda nome e datas, NAO mexe em valor/juros ---
        r = cliente_http.put(f'/api/checks/{id_aberto}', headers=cab,
                             json={'vencimento': '2027-01-10', 'emitente': 'Nome Corrigido',
                                   'data_operacao': '2026-09-15', 'senha': SENHA})
        assert r.status_code == 200, r.data
        with app.app_context():
            c = db.session.get(Check, id_aberto)
            assert str(c.due_date) == '2027-01-10', c.due_date
            assert c.issuer_name == 'Nome Corrigido', c.issuer_name
            assert c.amount == 2000.0 and c.interest_amount == 200.0 and c.net_amount == 1800.0, \
                f"valor/juros NAO podem mudar na edicao: {c.amount}/{c.interest_amount}/{c.net_amount}"
            assert str(c.operation.operation_date) == '2026-09-15', c.operation.operation_date

            # a edicao e a acao em lote ficam registradas na auditoria
            from app.models.domain import AuditLog
            logs = [l.description for l in AuditLog.query.all()]
            assert any('Senha incorreta' in d for d in logs), logs
            assert any('confirmado com senha' in d for d in logs), logs
            assert any('TIROU DO CALCULO' in d for d in logs), logs

        print("OK: fora_do_calculo (por id e por filtro), lucro/carteira/inadimplencia "
              "respeitando a marca, edicao exigindo senha, ano invalido recusado, "
              "valor e juros intactos e tudo na auditoria.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
