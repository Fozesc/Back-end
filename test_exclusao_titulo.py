"""
Apagar um titulo desfaz no caixa o que ele lancou:
  - a previa (GET /checks/<id>/exclusao) mostra exatamente o que vai mudar e nao grava nada;
  - pede a senha; as linhas ligadas ao titulo (recebimento) saem;
  - a saida do borderô diminui o liquido do titulo e a comissao e' recalculada, com os totais;
  - o ultimo titulo leva o borderô inteiro (linhas do caixa e o borderô);
  - no boot, borderô que ficou sem titulos antes desta regra sai do caixa.
Banco SEPARADO (fozesc_teste_exclusao_titulo), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_exclusao_titulo.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_exclusao_titulo'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
_apps = []
_db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def main():
    global _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Operation, Transaction, User
        from app.services.operation_service import calcular_comissao, calcular_linha
        from werkzeug.security import generate_password_hash

        app = create_app()
        _apps.append(app)
        _db = db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Lucas')
            db.session.add(cli)
            db.session.commit()
            id_cli = cli.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        bb = lambda: round(http.get('/api/transactions/balances', headers=cab).get_json()['bruto']['bb_total'], 2)

        r = http.post('/api/operations', headers=cab, json={
            'client_id': id_cli, 'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0,
            'account_source': 'BB', 'comissao': 2, 'iof_enabled': True, 'iof_base': 0.38, 'iof_diario': 0.0082,
            'checks': [{'valor': 1000, 'vencimento': (HOJE + timedelta(days=30)).isoformat(), 'num_doc': '1'},
                       {'valor': 2000, 'vencimento': (HOJE + timedelta(days=60)).isoformat(), 'num_doc': '2'}]})
        assert r.status_code == 201, r.data
        op_id = r.get_json()['id']
        (j1, i1, l1), (j2, i2, l2) = (calcular_linha(1000, 30, 4, True, 0.38, 0.0082),
                                      calcular_linha(2000, 60, 4, True, 0.38, 0.0082))
        com_total, com_2 = calcular_comissao(j1 + j2, 2, 4), calcular_comissao(j2, 2, 4)
        assert bb() == round(-(l1 + l2) - com_total, 2), bb()
        with app.app_context():
            id1, id2 = [c.id for c in Check.query.filter_by(operation_id=op_id).order_by(Check.id)]

        # o titulo 1 foi recebido no BB: o recebimento e' dele
        assert http.patch(f'/api/checks/{id1}/status', headers=cab,
                          json={'status': 'Pago', 'payment_data': {'method': 'BB'}}).status_code == 200
        antes = bb()

        # ------------------------------------------------ previa: mostra e nao grava
        r = http.get(f'/api/checks/{id1}/exclusao', headers=cab)
        assert r.status_code == 200, r.data
        previa = r.get_json()
        mudou = {c['descricao'].split(' - ')[0]: (c['tipo'], c['de'], c['para']) for c in previa['caixa']}
        assert mudou == {
            'Recebimento Cheque #1': ('entrada', 1000.0, None),
            'Cliente recebe': ('saida', round(l1 + l2, 2), l2),
            'Comissão (50% dos juros)': ('saida', com_total, com_2),
        }, mudou
        assert previa['efeito_saldo'] == round(-1000 + l1 + com_total - com_2, 2), previa
        assert previa['bordero_apagado'] is False and previa['bordero'] == op_id
        assert bb() == antes, 'a previa nao pode mudar o caixa'
        with app.app_context():
            assert db.session.get(Check, id1) is not None

        # ------------------------------------------------ senha
        assert http.delete(f'/api/checks/{id1}', headers=cab).status_code == 403
        assert http.delete(f'/api/checks/{id1}', headers=cab, json={'senha': 'errada'}).status_code == 403
        assert bb() == antes
        r = http.delete(f'/api/checks/{id1}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json() == previa, (r.get_json(), previa)
        assert bb() == round(antes + previa['efeito_saldo'], 2) == round(-l2 - com_2, 2), bb()
        assert http.get(f'/api/checks/{id1}/exclusao', headers=cab).status_code == 404

        with app.app_context():
            op = db.session.get(Operation, op_id)
            assert (op.total_face_value, op.total_interest, op.total_net_value, op.iof_amount, op.comissao_valor) == \
                (2000, j2, l2, i2, com_2), (op.total_face_value, op.total_interest, op.total_net_value, op.iof_amount)
            info = Transaction.query.filter_by(operation_id=op_id, category='Informativo').one()
            assert info.valor_informativo == round(l2 + com_2, 2), 'total do borderô = cliente + comissao'
            assert Transaction.query.filter_by(check_id=id1).count() == 0

        # ------------------------------------------------ ultimo titulo leva o borderô
        r = http.delete(f'/api/checks/{id2}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['bordero_apagado'] is True, r.data
        assert bb() == 0, 'sem titulo nenhum o caixa volta ao que era antes do borderô'
        with app.app_context():
            assert db.session.get(Operation, op_id) is None
            assert Transaction.query.filter_by(operation_id=op_id).count() == 0
            logs = [l.description for l in AuditLog.query.filter_by(target='Cheque')]
            assert sum('[confirmado com senha]' in d for d in logs) == 2 and any('Senha incorreta' in d for d in logs), logs
            assert any('ficou sem títulos e foi apagado' in d for d in logs), logs

            # borderô que ficou sem titulos antes desta regra: o boot tira a saida do caixa
            velho = Operation(client_id=id_cli, operation_date=HOJE, monthly_rate=4)
            db.session.add(velho)
            db.session.flush()
            db.session.add(Transaction(date=HOJE, description=f'Pgto Borderô #{velho.id} - Lucas', amount=-500,
                                       type='saida', origin='Sistema (BB)', category='Compra de Ativos',
                                       operation_id=velho.id))
            db.session.add(Transaction(date=HOJE, description='aporte', amount=900, type='entrada',
                                       origin='BB', category='Aporte'))
            db.session.commit()
            id_velho = velho.id
        assert bb() == 400
        _apps.append(create_app())
        assert bb() == 900, 'a saida do borderô sem titulos saiu no boot'
        with app.app_context():
            assert db.session.get(Operation, id_velho) is None
            assert AuditLog.query.filter(AuditLog.description.like('Borderô sem títulos%')).count() == 1

        print("OK: previa igual a exclusao e sem gravar nada, senha obrigatoria, recebimento do titulo sai, "
              "saida do borderô e comissao diminuem a parte dele (totais juntos), ultimo titulo leva o borderô "
              "e o boot limpa borderô que ficou sem titulos.")
    finally:
        for a in _apps:
            with a.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
