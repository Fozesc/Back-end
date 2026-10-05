"""
Receber com imposto (juros + IOF), prorrogado como Aguardando e multa da devolucao.

  1. o imposto so entra quando a tela manda (atrasado ou nao); a conta e' a da tela
     (calcularImposto em Front-end/src/utils/calculoBordero.js) e o caixa ganha uma linha
     'Imposto' separada do recebimento do titulo, na conta certa;
  2. dividido em partes: as partes fecham com titulo + imposto e cada parte paga
     primeiro o imposto; a quebra volta certa para a tela de detalhes;
  3. entrada invalida e' recusada sem gravar; desfazer a baixa tira tudo do caixa;
  4. "Prorrogado" deixou de ser status: o boot converte os antigos para Aguardando (uma
     vez, com auditoria), a prorrogacao nova grava Aguardando e a tela marca pelo historico;
  5. receber um cheque Devolvido nao apaga mais a multa que ja entrou no caixa.

Roda em um banco SEPARADO (fozesc_teste_imposto), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_recebimento_imposto.py
"""
import json
import os
import re
import subprocess
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_imposto'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
UTIL = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Front-end', 'src', 'utils', 'calculoBordero.js'))
_app = _db = None


def sql_admin(*comandos):
    conn = psycopg2.connect(URL_REAL)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    for c in comandos:
        cur.execute(c)
    cur.close()
    conn.close()


def tela(**entrada):
    """O imposto que a tela calcula (mesma funcao que o RecebimentoModal usa)."""
    codigo = (f"import('{UTIL}').then(m => console.log(JSON.stringify("
              f"m.calcularImposto({json.dumps(entrada)}))))")
    return json.loads(subprocess.run(['node', '-e', codigo], capture_output=True, text=True, check=True).stdout)


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import (AuditLog, Check, CheckExtension, Client, CompanySettings,
                                       Operation, Transaction, User)
        from app.services.transaction_service import TransactionService
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        hoje = date.today()

        with app.app_context():
            db.session.add(CompanySettings(company_name='Fozesc Teste'))
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA), role='Admin', active=True))
            cli = Client(name='Cliente Teste', standard_rate=5.0)
            db.session.add(cli)
            db.session.flush()
            op = Operation(client_id=cli.id, client_name_snapshot=cli.name, operation_date=hoje - timedelta(days=60),
                           total_face_value=9900.0, total_interest=0.0, total_net_value=9900.0,
                           iof_amount=10.0, status='Finalizada')
            db.session.add(op)
            db.session.flush()

            def cheque(num, valor, venc, status='Aguardando'):
                c = Check(operation_id=op.id, number=num, issuer_name=f'Emitente {num}', amount=valor,
                          interest_amount=0.0, net_amount=valor, due_date=venc, original_due_date=venc,
                          status=status)
                db.session.add(c)
                db.session.flush()
                return c.id

            id_atrasado = cheque('101', 2500.0, hoje - timedelta(days=20))
            id_em_dia = cheque('102', 1000.0, hoje + timedelta(days=10))
            id_devolvido = cheque('103', 800.0, hoje - timedelta(days=5))
            id_prorrogar = cheque('104', 1200.0, hoje + timedelta(days=3))
            id_legado = cheque('105', 1100.0, hoje + timedelta(days=13), status='Prorrogado')
            db.session.add(CheckExtension(check_id=id_legado, old_due_date=hoje - timedelta(days=17),
                                          new_due_date=hoje + timedelta(days=13), days_added=30, fee_amount=100.0))
            db.session.commit()

        # ------------------------------------- 4a. boot converte o Prorrogado antigo, uma vez
        for outro in (create_app(), create_app()):
            with outro.app_context():
                db.engine.dispose()     # senao o DROP DATABASE do fim acha conexao aberta
        with app.app_context():
            assert db.session.get(Check, id_legado).status == 'Aguardando'
            conversao = [l.description for l in AuditLog.query.filter(AuditLog.description.like('Status Prorrogado%'))]
            assert conversao == [f"Status Prorrogado virou Aguardando em 1 cheque(s) (ids: {id_legado}). "
                                 "A prorrogação continua no histórico de cada cheque."], conversao

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def baixa(check_id, status, payment_data=None):
            return http.patch(f'/api/checks/{check_id}/status', headers=cab,
                              json={'status': status, 'payment_data': payment_data or {}})

        def linhas(check_id):
            with app.app_context():
                return sorted((t.category, t.amount, t.origin) for t in Transaction.query.filter_by(check_id=check_id))

        def saldos():
            with app.app_context():
                return TransactionService().get_balances()['bruto']

        def item(check_id, status=''):
            r = http.get(f'/api/checks/?per_page=40&status={status}', headers=cab)
            assert r.status_code == 200, r.data
            return next(i for i in r.get_json()['items'] if i['id'] == check_id)

        legado = item(id_legado)
        assert legado['status'] == 'Aguardando' and legado['prorrogacoes'] == 1, legado

        # ------------------------------------------------- 3. entrada invalida: nada grava
        for ruim in ({'method': 'BB', 'imposto': -5},
                     {'method': 'BB', 'imposto': 'abc'},
                     {'method': 'BB', 'imposto': 10, 'imposto_calculo': 'lixo'},
                     {'method': 'BB', 'imposto': 10, 'imposto_calculo': {'dias': 20, 'taxa_mensal': 500, 'calculado': 9}},
                     {'partes': [{'conta': 'BB', 'valor': 2500}], 'imposto': 50}):
            r = baixa(id_atrasado, 'Pago', ruim)
            assert r.status_code == 400, (ruim, r.data)
        assert 'total com imposto' in r.get_json()['error'], r.get_json()
        assert linhas(id_atrasado) == [] and item(id_atrasado)['status'] == 'Atrasado'

        # ------------------------- 1. atrasado 20 dias, imposto calculado pela tela, uma conta
        venc = (hoje - timedelta(days=20)).isoformat()
        t = tela(valor=2500, dataBase=venc, dataPagamento=hoje.isoformat(), taxaMensal=5, diasCompensacao=0,
                 iofEnabled=True, iofBase=0.38, iofDiario=0.0082)
        assert t['dias'] == 20 and t['encargos'] > 0 and round(t['juros'] + t['iof'], 2) == t['encargos'], t
        bb_antes = saldos()['bb_total']
        r = baixa(id_atrasado, 'Pago', {'method': 'BB', 'forma': 'PIX', 'imposto': t['encargos'],
                                        'imposto_calculo': {'data_base': venc, 'dias': 20, 'taxa_mensal': 5,
                                                            'iof': True, 'calculado': t['encargos']}})
        assert r.status_code == 200, r.data
        assert linhas(id_atrasado) == [('Multas e Juros', t['encargos'], 'BB'), ('Recebimento de Cheque', 2500.0, 'BB')]
        assert round(saldos()['bb_total'] - bb_antes, 2) == round(2500 + t['encargos'], 2)
        g = item(id_atrasado)
        assert g['status'] == 'Pago' and g['imposto_cobrado'] == t['encargos'], g
        assert g['valor_pago'] == round(2500 + t['encargos'], 2), g
        with app.app_context():
            desc = Transaction.query.filter_by(check_id=id_atrasado, category='Multas e Juros').one().description
            assert desc == 'Imposto Cheque #101 - Emitente 101 (20 dias a 5% a.m. + IOF) · PIX', desc
            log = AuditLog.query.filter(AuditLog.action == 'BAIXA').order_by(AuditLog.id.desc()).first().description
            assert f"imposto R$ {t['encargos']:.2f}: 20 dias a 5% a.m. + IOF" in log, log

        # --- 3b. desfazer a baixa tira titulo E imposto do caixa
        r = baixa(id_atrasado, 'Aguardando')
        assert r.status_code == 200, r.data
        assert linhas(id_atrasado) == [] and round(saldos()['bb_total'], 2) == round(bb_antes, 2)
        g = item(id_atrasado)
        assert (g['imposto_cobrado'], g['valor_pago']) == (0.0, 0.0), g

        # ---------- 2. em dia, imposto digitado a mao, dividido: a 1a parte paga o imposto primeiro
        r = baixa(id_em_dia, 'Pago', {'imposto': 30, 'imposto_calculo': {'dias': -10, 'taxa_mensal': 5, 'calculado': 0},
                                      'partes': [{'conta': 'BB', 'forma': 'PIX', 'valor': 600},
                                                 {'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 430}]})
        assert r.status_code == 200, r.data
        assert linhas(id_em_dia) == sorted([('Multas e Juros', 30.0, 'BB'), ('Recebimento de Cheque', 570.0, 'BB'),
                                            ('Recebimento de Cheque', 430.0, 'Dinheiro')])
        g = item(id_em_dia)
        assert g['partes_pagamento'] == [{'conta': 'BB', 'forma': 'PIX', 'valor': 600.0},
                                         {'conta': 'Dinheiro', 'forma': '', 'valor': 430.0}], g['partes_pagamento']
        assert g['valor_pago'] == 1030.0 and g['imposto_cobrado'] == 30.0
        with app.app_context():
            desc = Transaction.query.filter_by(check_id=id_em_dia, category='Multas e Juros').one().description
            assert desc == 'Imposto Cheque #102 - Emitente 102 (valor à mão) (Parte 1/2 · PIX)', desc

        # ------------------------- 5. devolucao: taxa validada; receber depois mantem a multa
        r = baixa(id_devolvido, 'Devolvido', {'method': 'Dinheiro', 'taxa_multa': 150})
        assert r.status_code == 400 and linhas(id_devolvido) == [], r.data
        r = baixa(id_devolvido, 'Devolvido', {'method': 'Dinheiro', 'taxa_multa': 2})
        assert r.status_code == 200, r.data
        assert item(id_devolvido)['multa'] == 16.0
        r = baixa(id_devolvido, 'Pago', {'method': 'Caixa'})
        assert r.status_code == 200, r.data
        assert linhas(id_devolvido) == [('Multas e Juros', 16.0, 'Dinheiro'), ('Recebimento de Cheque', 800.0, 'Caixa')], \
            "a multa ja entrou no caixa: receber o cheque nao pode apaga-la"
        assert item(id_devolvido)['multa'] == 16.0

        # ------------------- 4b. prorrogacao nova fica Aguardando; vencida vira Atrasado sozinha
        r = http.post(f'/api/checks/{id_prorrogar}/prorrogate', headers=cab,
                      json={'prorrogar': True, 'new_date': (hoje + timedelta(days=33)).isoformat(),
                            'novos_juros': 60.0, 'taxa_mensal': 5, 'dias_compensacao': 2, 'iof': False,
                            'valor_recebido': 60.0, 'conta': 'Dinheiro'})
        assert r.status_code == 200, r.data
        assert (r.get_json()['status'], r.get_json()['prorrogacoes']) == ('Aguardando', 1), r.get_json()
        assert item(id_prorrogar, 'Aguardando')['prorrogacoes'] == 1
        with app.app_context():
            db.session.get(Check, id_prorrogar).due_date = hoje - timedelta(days=1)
            db.session.commit()
        assert item(id_prorrogar, 'Atrasado')['status'] == 'Atrasado'
        with app.app_context():
            assert Check.query.filter_by(status='Prorrogado').count() == 0

        print("OK: imposto so quando a tela manda, pela conta da tela, em linha propria no caixa (uma conta ou "
              "dividido com a 1a parte pagando o imposto), quebra das partes certa, entrada invalida barrada, "
              "desfazer limpa tudo; Prorrogado antigo vira Aguardando uma vez com auditoria e a prorrogacao nova "
              "ja grava Aguardando (vencida aparece Atrasado); taxa da multa validada e receber o devolvido "
              "mantem a multa.")
    finally:
        if _db is not None and _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
