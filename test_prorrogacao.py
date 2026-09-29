"""
Prorrogacao (backend):

  1. juros da prorrogacao = conta do Borderô de Liquido (Inverso) sobre o saldo devido,
     pega da propria tela (Front-end/src/utils/calculoBordero.js);
  2. o que o cliente paga quita primeiro esses juros e o que passar abate o saldo;
     o normal - pagar so os juros - deixa o valor devido igual; pagar menos que os
     juros soma a diferenca no valor devido;
  3. cada parte cai no caixa na conta certa, ligada ao cheque, e o historico grava o
     que a tela mostrou; a tela de detalhes traz o borderô de origem;
  4. Receber o resto e desfazer nao apaga os pagamentos parciais;
  5. entrada invalida e' recusada sem gravar nada; ajuste manual de saldo pede senha;
  6. "total operado" dos relatorios continua com o valor de quando o cheque entrou.

Roda em um banco SEPARADO (fozesc_teste_prorrogacao), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_prorrogacao.py
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

BANCO_TESTE = 'fozesc_teste_prorrogacao'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
IOF = {'iofEnabled': True, 'iofBase': 0.38, 'iofDiario': 0.0082}
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
    """O que a tela calcula (mesma funcao que o ProrrogacaoModal usa)."""
    codigo = (f"import('{UTIL}').then(m => console.log(JSON.stringify("
              f"m.calcularProrrogacao({json.dumps({**IOF, 'taxaMensal': 4, 'diasCompensacao': 2, **entrada})}))))")
    return json.loads(subprocess.run(['node', '-e', codigo], capture_output=True, text=True, check=True).stdout)


def main():
    global _app, _db
    sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}', f'CREATE DATABASE {BANCO_TESTE}')
    try:
        from app import create_app, db
        from app.models.domain import (AuditLog, Check, CheckExtension, Client, CompanySettings,
                                       Operation, Transaction, User)
        from app.services.dashboard_service import DashboardService
        from app.services.report_service import ReportService
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
            cli = Client(name='Cliente Teste', standard_rate=4.0)
            db.session.add(cli)
            db.session.flush()
            op = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                           operation_date=date(2026, 9, 1), monthly_rate=4.0, compensation_days=2,
                           total_face_value=1600.0, total_interest=60.0, total_net_value=1540.0,
                           account_source='BB', status='Finalizada', notes='borderô de teste')
            db.session.add(op)
            db.session.flush()
            a = Check(operation_id=op.id, number='101', bank='BB', due_date=date(2026, 10, 1), days=32,
                      amount=1000.0, interest_amount=60.0, net_amount=940.0, status='Aguardando',
                      issuer_name='Emitente A')
            b = Check(operation_id=op.id, number='102', due_date=date(2026, 10, 1), amount=500.0,
                      interest_amount=0.0, net_amount=500.0, status='Aguardando', issuer_name='Emitente B')
            c = Check(operation_id=op.id, number='103', due_date=date(2026, 9, 20), amount=100.0,
                      interest_amount=0.0, net_amount=100.0, status='Pago', issuer_name='Emitente C')
            d_ = Check(operation_id=op.id, number='104', due_date=date(2026, 10, 1), amount=1000.0,
                       interest_amount=0.0, net_amount=1000.0, status='Aguardando', issuer_name='Emitente D')
            db.session.add_all([a, b, c, d_])
            db.session.commit()
            id_a, id_b, id_c, id_d = a.id, b.id, c.id, d_.id

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}

        def prorrogar(check_id, corpo):
            return http.post(f'/api/checks/{check_id}/prorrogate', headers=cab, json=corpo)

        def como_a_tela(p, de, para, conta='BB'):
            """Monta o JSON exatamente como o ProrrogacaoModal manda."""
            return {'prorrogar': True, 'new_date': para, 'data_base': de, 'taxa_mensal': 4,
                    'dias_compensacao': 2, 'iof': True, 'novos_juros': p['juros'],
                    'juros_calculado': p['calculado']['encargos'], 'valor_recebido': p['pago'],
                    'conta': conta, 'data_recebimento': hoje.isoformat()}

        def estado(check_id):
            with app.app_context():
                c = db.session.get(Check, check_id)
                return {'amount': c.amount, 'juros': c.juros_pendentes, 'venc': c.due_date,
                        'status': c.status, 'orig': c.original_amount,
                        'tx': Transaction.query.count(), 'ext': CheckExtension.query.count()}

        def linhas_caixa(check_id):
            with app.app_context():
                return sorted((t.category, t.amount, t.origin) for t in Transaction.query.filter_by(check_id=check_id))

        def saldos():
            with app.app_context():
                return TransactionService().get_balances()['bruto']

        # ---------------- 1. o normal: prorroga pagando so os juros -> valor devido igual
        p1 = tela(valorAnterior=1000, dataBase='2026-10-01', novaData='2026-10-31')
        assert p1['pago'] == p1['juros'] > 0 and p1['novoTotal'] == 1000
        bb_antes = saldos()['bb_total']
        r = prorrogar(id_a, como_a_tela(p1, '2026-10-01', '2026-10-31'))
        assert r.status_code == 200, r.data
        g = r.get_json()
        assert g['valor_bruto'] == 1000 and g['vencimento'] == '2026-10-31' and g['status'] == 'Prorrogado'
        assert estado(id_a)['orig'] is None, "valor nao mudou: original continua vazio"
        h = g['historico_prorrogacao'][-1]
        assert (h['valor_anterior'], h['novos_juros'], h['valor_recebido'], h['juros_pagos'], h['principal_abatido'],
                h['juros_nao_pagos'], h['novo_total'], h['dias'], h['conta']) \
            == (1000, p1['juros'], p1['juros'], p1['juros'], 0, 0, 1000, 32, 'BB'), h
        assert linhas_caixa(id_a) == [('Multas e Juros', p1['juros'], 'BB')]
        assert round(saldos()['bb_total'] - bb_antes, 2) == p1['juros'], "os juros nao entraram no BB"

        # ------- 2. exemplo do pedido: juros 100, paga 300 -> 100 de juros + 200 do saldo -> 800
        p2 = tela(valorAnterior=1000, dataBase='2026-10-31', novaData='2026-11-30', jurosManual=100, pago=300)
        assert (p2['totalComJuros'], p2['jurosPagos'], p2['abatido'], p2['novoTotal']) == (1100, 100, 200, 800)
        r = prorrogar(id_a, como_a_tela(p2, '2026-10-31', '2026-11-30', conta='Dinheiro'))
        assert r.status_code == 200, r.data
        g = r.get_json()
        assert g['valor_bruto'] == 800 and g['valor_original'] == 1000
        h = g['historico_prorrogacao'][-1]
        assert (h['total_com_juros'], h['juros_pagos'], h['principal_abatido'], h['novo_total']) == (1100, 100, 200, 800)
        assert h['juros_calculado'] == p2['calculado']['encargos'] != 100, "juros a mao: guarda o calculado"
        assert linhas_caixa(id_a) == sorted([('Multas e Juros', p1['juros'], 'BB'),
                                             ('Multas e Juros', 100.0, 'Dinheiro'),
                                             ('Recebimento Parcial', 200.0, 'Dinheiro')])

        # ------------- 3. a proxima prorrogacao calcula sobre 800 (sem pagar nada agora)
        p3 = tela(valorAnterior=800, dataBase='2026-11-30', novaData='2026-12-30', pago=0)
        r = prorrogar(id_a, como_a_tela(p3, '2026-11-30', '2026-12-30'))
        assert r.status_code == 200, r.data
        g = r.get_json()
        assert g['valor_bruto'] == p3['totalComJuros'] == round(800 + p3['juros'], 2)
        assert g['juros_pendentes'] == p3['juros'], "juros nao pagos ficam marcados no saldo"

        # ------------------ 4. paga menos que os juros: nao abate e o que falta soma
        e = estado(id_a)
        p4 = tela(valorAnterior=e['amount'], dataBase='2026-12-30', novaData='2027-01-29')
        pago = round(p4['juros'] - 5, 2)
        p4 = tela(valorAnterior=e['amount'], dataBase='2026-12-30', novaData='2027-01-29', pago=pago)
        r = prorrogar(id_a, como_a_tela(p4, '2026-12-30', '2027-01-29'))
        assert r.status_code == 200, r.data
        g = r.get_json()
        h = g['historico_prorrogacao'][-1]
        assert (h['juros_pagos'], h['principal_abatido'], h['juros_nao_pagos']) == (pago, 0, 5.0), h
        assert g['valor_bruto'] == round(e['amount'] + 5, 2), "o que faltou de juros soma no valor devido"
        assert g['juros_pendentes'] == round(e['juros'] + 5, 2)

        # ---------------------------------- 5. pagamento parcial sem prorrogar
        e = estado(id_a)
        r = prorrogar(id_a, {'prorrogar': False, 'valor_recebido': 100, 'conta': 'Caixa'})
        assert r.status_code == 200, r.data
        d = estado(id_a)
        assert d['amount'] == round(e['amount'] - 100, 2) and d['venc'] == e['venc'] and d['status'] == 'Prorrogado'
        assert d['juros'] == round(max(e['juros'] - 100, 0), 2)

        # -------- 5b. pagamento da prorrogacao DIVIDIDO: parte no dinheiro, parte no banco
        pd = tela(valorAnterior=1000, dataBase='2026-10-01', novaData='2026-10-31', pago=300)
        corpo = como_a_tela(pd, '2026-10-01', '2026-10-31')
        corpo.pop('conta')
        corpo['partes'] = [{'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 100},
                           {'conta': 'BB', 'forma': 'PIX', 'valor': 150}]          # soma 250, nao fecha
        antes_d = estado(id_d)
        r = prorrogar(id_d, corpo)
        assert r.status_code == 400 and estado(id_d) == antes_d, (r.status_code, r.data)
        corpo['partes'][1]['valor'] = 200
        r = prorrogar(id_d, corpo)
        assert r.status_code == 200, r.data
        h = r.get_json()['historico_prorrogacao'][-1]
        jd = pd['juros']
        assert h['conta'] == 'Múltiplo (Dinheiro + BB)' and len(h['partes']) == 2, h
        assert linhas_caixa(id_d) == sorted([('Multas e Juros', jd, 'Dinheiro'),
                                             ('Recebimento Parcial', round(100 - jd, 2), 'Dinheiro'),
                                             ('Recebimento Parcial', 200.0, 'BB')]), linhas_caixa(id_d)
        assert r.get_json()['valor_bruto'] == round(1000 + jd - 300, 2)

        # ----------- 6. Receber o resto e desfazer: os pagamentos parciais continuam no caixa
        parciais = estado(id_a)['tx']
        r = http.patch(f'/api/checks/{id_a}/status', headers=cab,
                       json={'status': 'Pago', 'payment_data': {'method': 'Dinheiro', 'amount': d['amount']}})
        assert r.status_code == 200, r.data
        assert estado(id_a)['tx'] == parciais + 1
        r = http.patch(f'/api/checks/{id_a}/status', headers=cab, json={'status': 'Aguardando'})
        assert r.status_code == 200, r.data
        assert estado(id_a)['tx'] == parciais, "desfazer a baixa apagou pagamento parcial"

        # ------------------------------------------ 7. tela de detalhes: borderô de origem
        r = http.get(f'/api/checks/{id_a}', headers=cab)
        assert r.status_code == 200, r.data
        det = r.get_json()
        assert det['bordero']['id'] and det['bordero']['taxa_mensal'] == 4.0 and det['bordero']['qtd_titulos'] == 4
        assert det['bordero']['conta_saida'] == 'BB' and det['bordero']['observacao'] == 'borderô de teste'
        assert [t["id"] for t in det["titulos_do_bordero"]] == [id_c, id_b, id_d, id_a], "ordem pelo vencimento de hoje"
        assert len(det['historico_prorrogacao']) == 5 and det['dias'] == 32 and det['data_entrada'] == '2026-09-01'
        assert http.get(f'/api/checks/{id_a}').status_code == 401
        assert http.get('/api/checks/999999', headers=cab).status_code == 404

        # ---------------------------------------------- 8. entradas invalidas: nada grava
        antes = estado(id_b)
        amanha = (hoje + timedelta(days=1)).isoformat()
        invalidos = [
            ({'prorrogar': False, 'valor_recebido': 600, 'conta': 'BB'}, 400),        # maior que o devido
            ({'prorrogar': False, 'valor_recebido': 500, 'conta': 'BB'}, 400),        # quita tudo: use Receber
            ({'prorrogar': True, 'new_date': '2026-11-02', 'novos_juros': 10,
              'valor_recebido': 510, 'conta': 'BB'}, 400),                            # quita saldo + juros
            ({'prorrogar': False, 'valor_recebido': -5, 'conta': 'BB'}, 400),
            ({'prorrogar': False, 'valor_recebido': 'abc', 'conta': 'BB'}, 400),
            ({'prorrogar': False, 'valor_recebido': 'nan', 'conta': 'BB'}, 400),
            ({'prorrogar': False, 'valor_recebido': 50, 'conta': 'PIX'}, 400),        # conta fora da lista
            ({'prorrogar': False, 'valor_recebido': 50, 'conta': 'BB', 'data_recebimento': amanha}, 400),
            ({'prorrogar': False, 'valor_recebido': 0}, 400),                         # nada para registrar
            ({'prorrogar': True, 'new_date': '2026-09-30', 'novos_juros': 1}, 400),   # antes do vencimento
            ({'prorrogar': True, 'new_date': '2026-13-45', 'novos_juros': 1}, 400),   # data invalida
            ({'prorrogar': True, 'new_date': '2026-11-01', 'data_base': '2026-11-05', 'novos_juros': 1}, 400),
            ({'prorrogar': True, 'new_date': '2026-11-01', 'novos_juros': -3}, 400),
            ({'prorrogar': True, 'new_date': '2026-11-01', 'novos_juros': 1, 'taxa_mensal': 500}, 400),
            ({'prorrogar': False, 'valor_recebido': 50, 'conta': 'BB', 'saldo_base': 0}, 400),
            ({'new_date': '2026-11-01', 'fee_amount': 10}, 400),                      # tela antiga em cache
            ({'prorrogar': False, 'saldo_base': 450}, 403),                           # ajuste sem senha
            ({'prorrogar': False, 'saldo_base': 450, 'senha': 'errada'}, 403),
        ]
        for corpo, esperado in invalidos:
            r = prorrogar(id_b, corpo)
            assert r.status_code == esperado, (corpo, r.status_code, r.data)
            assert estado(id_b) == antes, f"gravou algo com entrada invalida: {corpo}"
        assert prorrogar(id_c, {'prorrogar': False, 'valor_recebido': 10, 'conta': 'BB'}).status_code == 400
        assert prorrogar(999999, {'prorrogar': False, 'valor_recebido': 10}).status_code == 404
        assert http.post(f'/api/checks/{id_b}/prorrogate', json={}).status_code == 401

        # ----------------------------------------- ajuste manual com a senha certa
        r = prorrogar(id_b, {'prorrogar': False, 'saldo_base': 450, 'senha': SENHA, 'notes': 'acordo'})
        assert r.status_code == 200, r.data
        assert estado(id_b)['amount'] == 450 and estado(id_b)['tx'] == antes['tx'], "ajuste nao mexe no caixa"
        assert r.get_json()['historico_prorrogacao'][-1]['ajuste'] == -50

        with app.app_context():
            acoes = [a.action for a in AuditLog.query.order_by(AuditLog.id)]
            assert acoes.count('PRORROGACAO') == 5 and acoes.count('RECEBIMENTO PARCIAL') == 2, acoes
            assert acoes.count('NEGADO') == 2, "senha errada/ausente tem que ficar na auditoria"
            assert any('AJUSTE MANUAL' in (a.description or '') for a in AuditLog.query.all())

            # ------------------ 9. carteira usa o saldo de hoje; relatorio usa o de entrada
            carteira = DashboardService().get_dashboard_data()['kpis']['carteira']
            assert round(carteira, 2) == round(db.session.get(Check, id_a).amount + 450 + db.session.get(Check, id_d).amount, 2), carteira
            operado = ReportService()._resumo_lucro(date(2026, 9, 1), date(2026, 9, 30))
            linha = next(x for x in operado['linhas'] if x['label'] == 'Valor de face operado no período')
            assert linha['valor'] == 2600.0, linha

        print(f"OK: prorrogar pagando os juros ({p1['juros']:.2f}) deixa o valor igual; juros 100 + pago 300 -> "
              f"800; proxima prorrogacao sobre 800 ({p3['totalComJuros']:.2f}); pago menor que os juros soma a "
              f"diferenca; pagamento sem prorrogar; Receber/desfazer preserva os parciais; detalhes com o borderô; "
              f"18 entradas invalidas barradas sem gravar; ajuste so com senha; caixa, auditoria, carteira e "
              f"'total operado' certos.")
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
