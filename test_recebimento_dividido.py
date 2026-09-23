"""
Confere o RECEBIMENTO DIVIDIDO do cheque (parte no dinheiro, parte no banco):
soma que tem que fechar, uma linha no caixa por parte vinculada ao cheque,
saldo certo em cada conta, e desfazer a baixa limpando TODAS as partes.

Roda em um banco SEPARADO (fozesc_teste_recebimento), criado e apagado pelo
proprio teste - nao encosta nos dados reais.

Rode:  ./venv/bin/python test_recebimento_dividido.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_recebimento'
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
        from app.models.domain import (Check, Client, CompanySettings, Operation,
                                       Transaction, User)
        from app.services.transaction_service import TransactionService
        from werkzeug.security import generate_password_hash

        app = create_app()          # create_all + o ALTER do check_id
        _app, _db = app, db
        http = app.test_client()

        with app.app_context():
            from sqlalchemy import inspect
            colunas = [c['name'] for c in inspect(db.engine).get_columns('transactions')]
            assert 'check_id' in colunas, f"coluna nao criada: {colunas}"

            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA),
                                role='Admin', active=True))
            cli = Client(name='Cliente Teste')
            db.session.add(cli)
            db.session.flush()

            op = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                           operation_date=date(2026, 9, 1), total_face_value=600.0,
                           total_interest=0.0, total_net_value=600.0, status='Finalizada')
            db.session.add(op)
            db.session.flush()

            # dois cheques com MESMO emitente e SEM numero de propósito: e' o caso em
            # que desfazer a baixa pelo texto da descricao confundia um com o outro.
            a = Check(operation_id=op.id, due_date=date(2026, 10, 10), amount=200.0,
                      interest_amount=0.0, net_amount=200.0, status='Aguardando',
                      issuer_name='Emitente Igual')
            b = Check(operation_id=op.id, due_date=date(2026, 10, 20), amount=300.0,
                      interest_amount=0.0, net_amount=300.0, status='Aguardando',
                      issuer_name='Emitente Igual')
            c = Check(operation_id=op.id, due_date=date(2026, 11, 5), amount=100.0,
                      interest_amount=0.0, net_amount=100.0, status='Aguardando',
                      issuer_name='Emitente C', number='999')
            db.session.add_all([a, b, c])
            db.session.commit()
            id_a, id_b, id_c = a.id, b.id, c.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def baixa(check_id, corpo):
            return http.patch(f'/api/checks/{check_id}/status', headers=cab, json=corpo)

        def saldos():
            with app.app_context():
                return TransactionService().get_balances()['bruto']

        # ---------------------------------------------------- soma que NAO fecha
        r = baixa(id_a, {'status': 'Pago', 'payment_data': {'partes': [
            {'conta': 'BB', 'forma': 'PIX', 'valor': 100},
            {'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 50}]}})
        assert r.status_code == 400, f"150 num cheque de 200 tinha que ser recusado: {r.data}"
        assert 'fechar' in r.get_json()['error'], r.data
        with app.app_context():
            assert db.session.get(Check, id_a).status == 'Aguardando', "nao podia ter mudado"
            assert Transaction.query.count() == 0, "pedido recusado nao pode lancar no caixa"

        # ------------------------------------------- conta que nao existe no caixa
        r = baixa(id_a, {'status': 'Pago', 'payment_data': {'partes': [
            {'conta': 'PIX', 'valor': 200}]}})
        assert r.status_code == 400 and 'conta' in r.get_json()['error'].lower(), r.data

        r = baixa(id_a, {'status': 'Pago', 'payment_data': {'partes': [
            {'conta': 'BB', 'forma': 'Bitcoin', 'valor': 200}]}})
        assert r.status_code == 400 and 'forma' in r.get_json()['error'].lower(), r.data

        r = baixa(id_a, {'status': 'Pago', 'payment_data': {'partes': [
            {'conta': 'BB', 'valor': 250}, {'conta': 'Dinheiro', 'valor': -50}]}})
        assert r.status_code == 400, f"parte negativa tinha que ser recusada: {r.data}"

        # ------------------------------------------------ recebimento DIVIDIDO ok
        r = baixa(id_a, {'status': 'Pago', 'payment_data': {'partes': [
            {'conta': 'BB', 'forma': 'PIX', 'valor': 120},
            {'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 80}]}})
        assert r.status_code == 200, r.data

        with app.app_context():
            ch = db.session.get(Check, id_a)
            assert ch.status == 'Pago' and round(ch.paid_amount, 2) == 200.0, ch.paid_amount
            assert ch.payment_method.startswith('Múltiplo'), ch.payment_method

            partes = Transaction.query.filter_by(check_id=id_a).order_by(Transaction.id).all()
            assert len(partes) == 2, f"uma linha no caixa por parte: {len(partes)}"
            assert [p.origin for p in partes] == ['BB', 'Dinheiro'], [p.origin for p in partes]
            assert [round(p.amount, 2) for p in partes] == [120.0, 80.0]
            assert all(p.type == 'entrada' and p.category == 'Recebimento de Cheque' for p in partes)
            assert 'Parte 1/2 · PIX' in partes[0].description, partes[0].description
            # na parte em dinheiro a forma e' a propria conta, entao nao se repete
            assert partes[1].description.endswith('(Parte 2/2)'), partes[1].description

        s = saldos()
        assert round(s['bb_total'], 2) == 120.0 and round(s['dinheiro_total'], 2) == 80.0, s

        # a quebra volta na listagem (para a tela de detalhes do titulo)
        r = http.get('/api/checks/?per_page=40&status=Pago', headers=cab)
        assert r.status_code == 200, r.data
        item = next(i for i in r.get_json()['items'] if i['id'] == id_a)
        assert len(item['partes_pagamento']) == 2, item['partes_pagamento']
        assert item['partes_pagamento'][0] == {'conta': 'BB', 'forma': 'PIX', 'valor': 120.0}, \
            item['partes_pagamento']

        # ------------------------------ recebimento normal (uma conta) nao mudou
        r = baixa(id_b, {'status': 'Pago', 'payment_data': {'method': 'Caixa'}})
        assert r.status_code == 200, r.data
        with app.app_context():
            tx = Transaction.query.filter_by(check_id=id_b).all()
            assert len(tx) == 1 and tx[0].origin == 'Caixa', tx
            assert 'Parte' not in tx[0].description, tx[0].description
            ch = db.session.get(Check, id_b)
            assert ch.payment_method == 'Caixa' and round(ch.paid_amount, 2) == 300.0
        assert round(saldos()['caixa_total'], 2) == 300.0, saldos()

        # -------------------- desfazer a baixa dividida apaga TODAS as partes...
        r = baixa(id_a, {'status': 'Aguardando'})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert Transaction.query.filter_by(check_id=id_a).count() == 0, "sobrou parte no caixa"
            ch = db.session.get(Check, id_a)
            assert ch.paid_amount == 0.0 and ch.payment_date is None and ch.payment_method is None
        s = saldos()
        assert round(s['bb_total'], 2) == 0.0 and round(s['dinheiro_total'], 2) == 0.0, s
        # ...e NAO encosta no recebimento do outro cheque (mesmo emitente, sem numero)
        assert round(saldos()['caixa_total'], 2) == 300.0, "o cheque B nao podia ser afetado"
        with app.app_context():
            assert Transaction.query.filter_by(check_id=id_b).count() == 1

        # ------- multa de devolucao convive com taxa de prorrogacao (mesma categoria)
        r = http.post(f'/api/checks/{id_c}/prorrogate', headers=cab,
                      json={'new_date': '2026-12-05', 'fee_amount': 10.0, 'method': 'BB'})
        assert r.status_code == 200, r.data
        r = baixa(id_c, {'status': 'Devolvido', 'payment_data': {'method': 'Dinheiro', 'taxa_multa': 2.0}})
        assert r.status_code == 200, r.data
        with app.app_context():
            assert round(db.session.get(Check, id_c).fine_amount, 2) == 2.0
        r = baixa(id_c, {'status': 'Aguardando'})
        assert r.status_code == 200, r.data
        with app.app_context():
            restantes = Transaction.query.filter_by(check_id=id_c).all()
            assert len(restantes) == 1 and restantes[0].description.startswith('Taxa Prorrogação'), \
                f"a taxa de prorrogacao tinha que ficar: {[t.description for t in restantes]}"
            assert db.session.get(Check, id_c).fine_amount == 0.0

        # ------------------- lancamento ANTIGO (sem check_id) ainda e' desfeito
        with app.app_context():
            ch = db.session.get(Check, id_c)
            ch.status = 'Pago'
            ch.paid_amount = 100.0
            ch.payment_method = 'Dinheiro'
            ch.payment_date = date(2026, 11, 6)
            db.session.add(Transaction(
                date=date(2026, 11, 6),
                description=f"Recebimento Cheque #{ch.number} - {ch.issuer_name}",
                amount=100.0, type='entrada', origin='Dinheiro',
                category='Recebimento de Cheque', operation_id=ch.operation_id))  # sem check_id
            db.session.commit()
        r = baixa(id_c, {'status': 'Aguardando'})
        assert r.status_code == 200, r.data
        with app.app_context():
            antigos = Transaction.query.filter(
                Transaction.category == 'Recebimento de Cheque',
                Transaction.check_id.is_(None)).count()
            assert antigos == 0, "lancamento antigo (sem check_id) tinha que ser apagado"

        # --------------- apagar o cheque nao trava na FK nem leva o caixa embora
        r = http.delete(f'/api/checks/{id_b}', headers=cab)
        assert r.status_code == 200, r.data
        with app.app_context():
            sobrou = Transaction.query.filter(
                Transaction.description.like('Recebimento Cheque%'),
                Transaction.origin == 'Caixa').all()
            assert len(sobrou) == 1 and sobrou[0].check_id is None, \
                "o historico do caixa fica, com o vinculo em NULL"

        with app.app_context():
            from app.models.domain import AuditLog
            logs = [l.description for l in AuditLog.query.all()]
            assert any('Partes: BB R$ 120.00 (PIX) + Dinheiro R$ 80.00' in d for d in logs), logs

        print("OK: soma das partes tem que fechar, conta/forma validadas no backend, "
              "uma linha por parte vinculada ao cheque, saldo certo por conta, "
              "desfazer limpa todas as partes sem encostar em outro cheque, "
              "prorrogacao preservada, lancamento antigo ainda desfeito e auditoria completa.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
