"""
Comissao (prorrogacao e borderô) descontada de um vale. No caixa a comissao sai inteira e o
desconto entra como pagamento do vale na mesma conta, entao o saldo so perde o que passa do vale:
  A. comissao menor que o que falta no vale: desconta tudo, pagamento parcial, saldo nao muda;
  B. comissao igual ao que falta: quita o vale, saldo nao muda;
  C. comissao maior: quita o vale e so o que passar sai de verdade, na conta escolhida;
  sem vale continua igual; validacoes e pedido repetido nao gravam nada; o abatimento aparece
  no historico do vale e do titulo e na auditoria; desfazer o abatimento e apagar o titulo
  reabrem o vale.
Banco SEPARADO (fozesc_teste_abatimento), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_abatimento_vale.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_abatimento'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
HOJE = date.today()
D = lambda n: (HOJE + timedelta(days=n)).isoformat()
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
        from app.models.domain import AuditLog, Check, Client, CompanySettings, Operation, Transaction, User, Vale
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            cli = Client(name='Lucas')
            db.session.add(cli)
            db.session.flush()
            id_cli = cli.id
            op = Operation(client_id=cli.id, operation_date=HOJE - timedelta(days=20), monthly_rate=4)
            db.session.add(op)
            db.session.flush()
            ids = {}
            for num in ('A', 'B', 'C', 'D'):
                c = Check(operation_id=op.id, number=num, due_date=HOJE + timedelta(days=10), amount=1000.0,
                          interest_amount=0.0, net_amount=1000.0, status='Aguardando', issuer_name=f'Emitente {num}')
                db.session.add(c)
                db.session.flush()
                ids[num] = c.id
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        saldos = lambda: http.get('/api/transactions/balances', headers=cab).get_json()['bruto']
        vale = lambda vid: http.get(f'/api/vales/{vid}', headers=cab).get_json()
        linhas = lambda: Transaction.query.count()

        def novo_vale(descricao, valor):
            r = http.post('/api/vales', headers=cab, json={'descricao': descricao, 'valor': valor, 'conta': 'Dinheiro'})
            assert r.status_code == 201, r.data
            return r.get_json()['id']
        v1, v2, v3 = novo_vale('Adiantamento Daniel', 500), novo_vale('Adiantamento Ana', 100), novo_vale('Vale Joao', 50)

        def prorrogar(num, juros, vale_id=None, **extra):
            corpo = {'prorrogar': True, 'new_date': D(40), 'data_base': D(10), 'taxa_mensal': 4, 'dias_compensacao': 0,
                     'iof': False, 'novos_juros': juros, 'valor_recebido': juros, 'conta': 'BB',
                     'comissao': 2, 'comissao_base': juros, 'comissao_conta': 'Caixa',
                     **({'vale_id': vale_id} if vale_id is not None else {}), **extra}
            return http.post(f'/api/checks/{ids[num]}/prorrogate', headers=cab, json=corpo)

        # ------------------------------------------------ validacoes: nada e' gravado
        with app.app_context():
            antes = linhas()
        for extra, erro in (({'comissao': 0}, 'tem comissão'), ({'vale_id': 99999}, 'não encontrado'),
                            ({'vale_id': 'abc'}, 'inválido')):
            r = prorrogar('A', 100, **{'vale_id': v1, **extra})
            assert r.status_code == 400 and erro in r.get_json()['error'], (extra, r.data)
        with app.app_context():
            assert linhas() == antes and db.session.get(Check, ids['A']).due_date == HOJE + timedelta(days=10)
        assert vale(v1)['saldo'] == 500

        # -------------------------- A: comissao (50) menor que o vale (500): pagamento parcial
        r = prorrogar('A', 100, v1)
        assert r.status_code == 200, r.data
        s = saldos()
        assert (s['bb_total'], s['caixa_total']) == (100, 0), 'a comissao inteira foi para o vale: o saldo nao muda'
        with app.app_context():
            ls = Transaction.query.filter_by(check_id=ids['A']).order_by(Transaction.id).all()
            assert [(t.category, t.amount) for t in ls if t.valor_informativo is None] == [
                ('Multas e Juros', 100), ('Comissão', -50), ('Vale', 50)], 'comissao inteira sai e o desconto entra'
        a = vale(v1)
        assert (a['status'], a['pago'], a['saldo']) == ('Aberto', 50, 450), a
        p = a['pagamentos'][0]
        assert p['abatimento'] is True and p['valor'] == 50 and p['conta'] == 'Caixa', p
        assert p['descricao'].startswith(f'Desconto parcial no vale #{v1}') and 'cheque #A' in p['descricao'], p

        # pedido repetido (mesma confirmacao de novo): recusado, nada duplica
        with app.app_context():
            antes = linhas()
        assert prorrogar('A', 100, v1).status_code == 400
        with app.app_context():
            assert linhas() == antes
        assert vale(v1)['saldo'] == 450 and saldos()['bb_total'] == 100

        # -------------------------- B: comissao (50) igual ao que falta (50): quita
        r = prorrogar('B', 100, v3)
        assert r.status_code == 200, r.data
        b = vale(v3)
        assert (b['status'], b['saldo']) == ('Pago', 0), b
        assert b['pagamentos'][0]['descricao'].startswith(f'Desconto no vale #{v3} '), b
        assert saldos()['caixa_total'] == 0
        r = prorrogar('D', 100, v3)
        assert r.status_code == 400 and 'quitado' in r.get_json()['error'], r.data

        # -------------------------- C: comissao (200) maior que o vale (100): o resto sai do caixa
        r = prorrogar('C', 400, v2)
        assert r.status_code == 200, r.data
        c = vale(v2)
        assert (c['status'], c['pago'], c['saldo']) == ('Pago', 100, 0), c
        s = saldos()
        assert (s['bb_total'], s['caixa_total']) == (600, -100), 'so os 100 que passaram do vale sairam (da Caixa)'
        h = http.get(f"/api/checks/{ids['C']}", headers=cab).get_json()['historico_prorrogacao'][-1]
        assert (h['comissao_valor'], h['vale_id'], h['vale_abatido'], h['comissao_no_caixa'], h['comissao_conta']) == \
            (200, v2, 100, 100, 'Caixa'), h
        with app.app_context():
            com = Transaction.query.filter_by(check_id=ids['C'], category='Comissão').one()
            assert com.amount == -200 and f'R$ 100,00 descontados no vale #{v2}' in com.description, com.description
            grupo = {t.grupo_id for t in Transaction.query.filter_by(check_id=ids['C'])}
            assert len(grupo) == 1 and None not in grupo, 'abatimento lancado junto com a prorrogacao'

        # -------------------------- sem vale: como sempre (comissao inteira sai do caixa)
        r = prorrogar('D', 100)
        assert r.status_code == 200, r.data
        assert saldos()['caixa_total'] == -150
        assert http.get('/api/vales', headers=cab).get_json()['em_aberto'] == 450

        # o relatorio do caixa mostra a comissao e o desconto e bate com o saldo
        rel = http.get('/api/reports/caixa', headers=cab, query_string={'inicio': '', 'fim': '', 'conta': 'caixa'}).get_json()
        assert rel['resumo']['saldo_final'] == -150 and rel['extrato']['total'] == 7, rel['resumo']

        # auditoria: o titulo e o vale
        with app.app_context():
            logs = [l.description for l in AuditLog.query.filter_by(target='Vale').filter(
                AuditLog.description.like('Desconto no vale%'))]
            assert len(logs) == 3 and any(f'#{v2}' in d and 'de verdade saíram R$ 100.00' in d for d in logs), logs
            assert AuditLog.query.filter(AuditLog.target == 'Cheque',
                                         AuditLog.description.like(f'%100.00 descontados no vale #{v2} e R$ 100.00 saíram de Caixa%')).count() == 1

        # ------------- desfazer o desconto (apagar a linha): o vale volta a dever e a comissao fica paga em dinheiro
        abat_v3 = vale(v3)['pagamentos'][0]['id']
        assert http.delete(f'/api/transactions/{abat_v3}', headers=cab, json={'senha': SENHA}).status_code == 200
        assert (vale(v3)['status'], vale(v3)['saldo']) == ('Aberto', 50)
        assert saldos()['caixa_total'] == -200, 'sem o desconto, a comissao fica paga inteira em dinheiro'

        # -------------------------- apagar o titulo desfaz o abatimento que a comissao dele fez
        previa = http.get(f"/api/checks/{ids['C']}/exclusao", headers=cab).get_json()
        assert previa['vales'] == [{'vale': v2, 'valor': 100}], previa
        r = http.delete(f"/api/checks/{ids['C']}", headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json() == previa, r.data
        assert (vale(v2)['status'], vale(v2)['saldo']) == ('Aberto', 100)
        s = saldos()
        assert (s['bb_total'], s['caixa_total']) == (300, -100), 'saem os juros (400), a comissao (200) e o desconto (100) do C'

        # ================================================ borderô: a comissao nasce com ele
        from app.services.operation_service import calcular_comissao, calcular_linha
        vb, vp = novo_vale('Vale grande', 1000), novo_vale('Vale pequeno', 5)
        bb = lambda: round(saldos()['bb_total'], 2)

        def bordero(cheques, vale_id=None, comissao=2):
            return http.post('/api/operations', headers=cab, json={
                'client_id': id_cli,
                'operation_date': HOJE.isoformat(), 'taxa_mensal': 4, 'dias_compensacao': 0, 'account_source': 'BB',
                'comissao': comissao, 'iof_enabled': False,
                **({'vale_id': vale_id} if vale_id is not None else {}),
                'checks': [{'valor': v, 'vencimento': D(d), 'num_doc': f'B{i}'} for i, (v, d) in enumerate(cheques, 1)]})

        with app.app_context():
            ops_antes = Operation.query.count()
        r = bordero([(1000, 30)], vale_id=vb, comissao=0)
        assert r.status_code == 400 and 'tem comissão' in r.get_json()['error'], r.data
        with app.app_context():
            assert Operation.query.count() == ops_antes, 'borderô recusado nao grava nada'

        # comissao inteira no vale: do caixa so sai o cliente
        (j1, _, l1), (j2, _, l2) = calcular_linha(1000, 30, 4), calcular_linha(2000, 60, 4)
        com, com2 = calcular_comissao(j1 + j2, 2, 4), calcular_comissao(j2, 2, 4)
        antes = bb()
        r = bordero([(1000, 30), (2000, 60)], vale_id=vb)
        assert r.status_code == 201, r.data
        op1 = r.get_json()['id']
        assert bb() == round(antes - l1 - l2, 2), 'a comissao nao saiu do caixa'
        g = vale(vb)
        assert (g['status'], g['pago'], g['pagamentos'][0]['abatimento']) == ('Aberto', com, True), g
        assert f'Borderô #{op1}' in g['pagamentos'][0]['descricao']
        with app.app_context():
            ls = Transaction.query.filter_by(operation_id=op1).order_by(Transaction.id).all()
            assert [(t.category, t.amount) for t in ls] == [('Informativo', 0), ('Compra de Ativos', -round(l1 + l2, 2)),
                                                            ('Comissão', -com), ('Vale', com)], [(t.category, t.amount) for t in ls]
            assert ls[0].valor_informativo == round(l1 + l2, 2), 'total que sai do banco = so o cliente'
            assert len({t.grupo_id for t in ls}) == 1
            id_b1, id_b2 = [c.id for c in Check.query.filter_by(operation_id=op1).order_by(Check.id)]
        det = http.get(f'/api/checks/{id_b1}', headers=cab).get_json()['bordero']
        assert det['comissao_valor'] == com and det['comissao_vale'] == {'vale': vb, 'valor': com}, det

        # comissao maior que o vale: quita e o resto sai do caixa (da conta do borderô)
        (j3, _, l3) = calcular_linha(1000, 30, 4)
        antes = bb()
        r = bordero([(1000, 30)], vale_id=vp)
        assert r.status_code == 201, r.data
        c3 = calcular_comissao(j3, 2, 4)
        assert vale(vp)['status'] == 'Pago' and bb() == round(antes - l3 - (c3 - 5), 2)
        with app.app_context():
            com_linha = Transaction.query.filter_by(operation_id=r.get_json()['id'], category='Comissão').one()
            assert com_linha.amount == -c3 and 'R$ 5,00 descontados no vale' in com_linha.description

        # apagar um titulo: a comissao cai e o abatimento cai junto (o vale volta a dever a diferenca)
        antes = bb()
        r = http.delete(f'/api/checks/{id_b1}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['vales'] == [{'vale': vb, 'valor': round(com - com2, 2)}], r.data
        assert vale(vb)['pago'] == com2 and bb() == round(antes + l1, 2)
        # o ultimo leva o borderô e o abatimento inteiro
        r = http.delete(f'/api/checks/{id_b2}', headers=cab, json={'senha': SENHA})
        assert r.status_code == 200 and r.get_json()['bordero_apagado'] is True, r.data
        assert (vale(vb)['pago'], vale(vb)['saldo']) == (0, 1000) and bb() == round(antes + l1 + l2, 2)
        with app.app_context():
            assert AuditLog.query.filter(AuditLog.target == 'Vale',
                                         AuditLog.description.like('Desconto no vale%Borderô%')).count() == 2

        print("OK: comissao da prorrogacao e do borderô abate o vale (menor: parcial; igual: quita; maior: quita e "
              "so o resto sai do caixa na conta escolhida), sem vale igual a antes, validacoes e pedido repetido sem "
              "gravar, historico do vale e do titulo, relatorio batendo com o saldo, auditoria, desfazer e apagar o "
              "titulo reabrem o vale (no borderô o abatimento acompanha a comissao recalculada).")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
