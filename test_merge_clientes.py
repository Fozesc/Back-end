"""
Confere a mesclagem de clientes duplicados (ClientService.merge):

  - senha errada nao move nada e fica registrada como NEGADO;
  - senha certa move TODOS os borderos e cheques para o cliente escolhido,
    atualiza o nome que aparece no bordero e apaga o cadastro antigo;
  - nenhum cheque e perdido nem alterado (valor, juros e status intactos);
  - nao deixa juntar um cliente com ele mesmo;
  - a auditoria guarda o retrato do cadastro apagado (CPF, limite, taxa),
    que e o unico caminho de volta se voce juntar as pessoas erradas.

Roda num banco SEPARADO (fozesc_teste_merge), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_merge_clientes.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO = 'fozesc_teste_merge'
os.environ['DATABASE_URL'] = re.sub(r'/[^/]+$', f'/{BANCO}', URL_REAL)
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha@Teste123'
_app = _db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def semear():
    from app.models.domain import AuditLog, Check, Client, Operation, User
    from werkzeug.security import generate_password_hash
    db = _db
    user = User(name='Teste', email='teste@fozesc.com', role='Admin', active=True,
                password_hash=generate_password_hash(SENHA))
    # 'A' e o cadastro da planilha (sem CPF); 'B' e o de producao (completo)
    a = Client(name='Joao dos S. Ribeiro', credit_limit=0.0, standard_rate=4.0)
    b = Client(name='Joao dos Santos Ribeiro', document='123.456.789-00',
               phone='45 99999-0000', credit_limit=50000.0, standard_rate=3.5)
    db.session.add_all([user, a, b])
    db.session.flush()

    for n, (dia, valor, status) in enumerate([(1, 1000.0, 'Aguardando'),
                                              (2, 2000.0, 'Pago'),
                                              (3, 3000.0, 'Juridico')], 1):
        op = Operation(client_id=a.id, client_name_snapshot=a.name,
                       operation_date=date(2026, 3, dia), monthly_rate=0.0,
                       compensation_days=0, iof_amount=0.0, total_face_value=valor,
                       total_interest=100.0, total_net_value=valor - 100, status='Finalizada')
        db.session.add(op)
        db.session.flush()
        db.session.add(Check(operation_id=op.id, type='CHEQUE', bank='Sicredi', number=str(n),
                             due_date=date(2026, 6, dia), original_due_date=date(2026, 6, dia),
                             amount=valor, interest_amount=100.0, net_amount=valor - 100,
                             days=90, status=status, destination_bank='Carteira',
                             fora_do_calculo=(status == 'Pago')))
    # um borderô que ja era do B, para provar que o dele nao e tocado
    op_b = Operation(client_id=b.id, client_name_snapshot=b.name,
                     operation_date=date(2026, 4, 1), monthly_rate=0.0, compensation_days=0,
                     iof_amount=0.0, total_face_value=500.0, total_interest=0.0,
                     total_net_value=500.0, status='Finalizada')
    db.session.add(op_b)
    db.session.commit()
    return user.id, a.id, b.id


def main():
    global _app, _db
    assert BANCO not in URL_REAL, "o banco de teste tem o mesmo nome do banco real"
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO}', f'CREATE DATABASE {BANCO}')
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, Check, Client, Operation
        from app.services.client_service import ClientService
        _app, _db = create_app(), db
        with _app.app_context():
            user_id, a_id, b_id = semear()
            service = ClientService()
            # o merge confere a senha do usuario logado; aqui fingimos o login
            service._usuario_logado = lambda: db.session.get(
                __import__('app.models.domain', fromlist=['User']).User, user_id)

            cheques_antes = {c.id: (c.amount, c.interest_amount, c.status, c.fora_do_calculo)
                             for c in Check.query.all()}

            # 1. senha errada nao pode mover nada
            try:
                service.merge(a_id, b_id, 'senha errada')
                raise AssertionError("merge aceitou senha errada")
            except PermissionError:
                pass
            db.session.rollback()
            assert Client.query.get(a_id) is not None, "senha errada apagou o cliente"
            assert Operation.query.filter_by(client_id=a_id).count() == 3, \
                "senha errada mexeu nos borderos"
            assert AuditLog.query.filter_by(action='NEGADO').count() == 1, \
                "senha errada nao foi registrada na auditoria"

            # 2. nao deixa juntar com ele mesmo
            try:
                service.merge(a_id, a_id, SENHA)
                raise AssertionError("deixou juntar o cliente com ele mesmo")
            except ValueError:
                pass

            # 3. merge de verdade
            r = service.merge(a_id, b_id, SENHA)
            assert r['borderos'] == 3 and r['cheques'] == 3, r

            assert Client.query.get(a_id) is None, "o cadastro antigo nao foi apagado"
            assert Client.query.get(b_id) is not None, "apagou o cadastro errado"
            assert Operation.query.filter_by(client_id=b_id).count() == 4, \
                "os borderos nao foram todos para o cliente escolhido"
            assert Operation.query.filter(
                Operation.client_name_snapshot == 'Joao dos S. Ribeiro').count() == 0, \
                "o nome antigo ficou no bordero"

            # 4. nenhum cheque perdido nem alterado
            cheques_depois = {c.id: (c.amount, c.interest_amount, c.status, c.fora_do_calculo)
                              for c in Check.query.all()}
            assert cheques_depois == cheques_antes, "algum cheque foi alterado ou perdido"

            # 5. o cadastro que ficou nao foi mexido
            b = Client.query.get(b_id)
            assert b.document == '123.456.789-00' and b.credit_limit == 50000.0 \
                and b.standard_rate == 3.5, "o cadastro mantido foi alterado"

            # 6. auditoria guarda o caminho de volta
            log = AuditLog.query.filter_by(action='MERGE').first()
            assert log is not None, "merge nao foi registrado na auditoria"
            for pedaco in ("Joao dos S. Ribeiro", "3 borderô", "3 cheque", "confirmado com senha"):
                assert pedaco in log.description, f"auditoria sem '{pedaco}': {log.description}"

            print("OK: senha errada barrada e registrada, 3 borderôs e 3 cheques movidos, "
                  "cadastro antigo apagado, nenhum cheque alterado, auditoria completa.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO}')


if __name__ == '__main__':
    main()
