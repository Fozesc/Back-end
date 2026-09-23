"""
Confere o relatorio gerencial da tela (GET /api/reports/resumo).

Antes a tela de Relatorios NAO chamava o backend: imprimia valores fixos escritos
no Vue (Saldo Anterior R$ 1.000,00, Entradas R$ 15.000,00...) iguais para qualquer
tipo e qualquer periodo. Este teste prova que agora cada numero vem do banco e que
bate com o criterio que o Dashboard e o Historico Mensal ja usam.

Roda em um banco SEPARADO (fozesc_teste_relatorio), criado e apagado pelo proprio
teste - nao encosta nos dados reais.

Rode:  ./venv/bin/python test_relatorios.py
"""
import os
import re
from datetime import date

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_relatorio'
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


def valores(dados):
    """{label: valor} para conferir linha a linha sem depender da ordem."""
    return {l['label']: l['valor'] for l in dados['linhas']}


def main():
    global _app, _db
    recria_banco()
    try:
        from app import create_app, db
        from app.models.domain import (Check, Client, Operation, Transaction,
                                       CompanySettings, User)
        from werkzeug.security import generate_password_hash

        app = create_app()
        _app, _db = app, db
        http = app.test_client()

        with app.app_context():
            db.session.add(User(name='Teste', email='teste@fozesc.com',
                                password_hash=generate_password_hash(SENHA),
                                role='Admin', active=True))
            db.session.add(CompanySettings(company_name='Fozesc Teste', cnpj='00.000.000/0001-00'))

            cli = Client(name='Cliente Teste')
            db.session.add(cli)
            db.session.flush()

            # Bordero DENTRO do periodo do relatorio (junho/2026)
            op_dentro = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                                  operation_date=date(2026, 6, 10), iof_amount=37.58,
                                  total_face_value=3000.0, total_interest=300.0,
                                  total_net_value=2662.42, status='Finalizada')
            # Bordero FORA do periodo (marco/2026) - nao pode entrar em nada
            op_fora = Operation(client_id=cli.id, client_name_snapshot=cli.name,
                                operation_date=date(2026, 3, 10), iof_amount=99.0,
                                total_face_value=5000.0, total_interest=500.0,
                                total_net_value=4401.0, status='Finalizada')
            db.session.add_all([op_dentro, op_fora])
            db.session.flush()

            db.session.add_all([
                # pago DENTRO do periodo -> entra no "juros recebido"
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 20),
                      payment_date=date(2026, 6, 25), amount=1000.0,
                      interest_amount=100.0, net_amount=900.0, status='Pago',
                      issuer_name='Emitente Pago', number='111'),
                # pago FORA do periodo -> nao entra no "juros recebido"
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 21),
                      payment_date=date(2026, 8, 5), amount=800.0,
                      interest_amount=80.0, net_amount=720.0, status='Pago',
                      issuer_name='Emitente Pago Depois', number='112'),
                # juridico -> inadimplencia
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 15),
                      amount=500.0, interest_amount=50.0, net_amount=450.0,
                      status='Juridico', issuer_name='Emitente Juridico', number='333'),
                # devolvido -> inadimplencia
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 16),
                      amount=200.0, interest_amount=20.0, net_amount=180.0,
                      status='Devolvido', issuer_name='Emitente Devolvido', number='444'),
                # aguardando -> NAO e inadimplencia
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 28),
                      amount=700.0, interest_amount=70.0, net_amount=630.0,
                      status='Aguardando', issuer_name='Emitente Aberto', number='555'),
                # historico (fora_do_calculo): pago e juridico no periodo, nao pode
                # aparecer em NENHUM numero do relatorio
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 18),
                      payment_date=date(2026, 6, 19), amount=9999.0,
                      interest_amount=999.0, net_amount=9000.0, status='Pago',
                      fora_do_calculo=True, issuer_name='Historico Pago', number='666'),
                Check(operation_id=op_dentro.id, due_date=date(2026, 6, 17),
                      amount=8888.0, interest_amount=888.0, net_amount=8000.0,
                      status='Juridico', fora_do_calculo=True,
                      issuer_name='Historico Juridico', number='777'),
                # cheque do bordero de fora do periodo
                Check(operation_id=op_fora.id, due_date=date(2026, 3, 20),
                      amount=5000.0, interest_amount=500.0, net_amount=4500.0,
                      status='Aguardando', issuer_name='Emitente Marco', number='888'),
            ])

            # Caixa: 1 movimento ANTES do periodo (vira saldo anterior) e 3 dentro.
            # amount negativo de proposito: o sistema tem linhas gravadas com sinal
            # trocado e o relatorio usa abs() por tipo, igual ao Fluxo de Caixa.
            db.session.add_all([
                Transaction(date=date(2026, 5, 10), description='Aporte', amount=1000.0,
                            type='entrada', origin='Dinheiro', category='Aporte'),
                Transaction(date=date(2026, 6, 5), description='Recebimento', amount=500.0,
                            type='entrada', origin='BB', category='Cheque'),
                Transaction(date=date(2026, 6, 6), description='Recebimento 2', amount=300.0,
                            type='entrada', origin='Caixa', category='Cheque'),
                Transaction(date=date(2026, 6, 7), description='Emprestimo', amount=-200.0,
                            type='saida', origin='Dinheiro', category='Bordero'),
            ])
            db.session.commit()

        r = http.post('/api/auth/login', json={'email': 'teste@fozesc.com', 'password': SENHA})
        assert r.status_code == 200, r.data
        cab = {'Authorization': f"Bearer {r.get_json()['token']}"}

        def resumo(tipo, inicio='2026-06-01', fim='2026-06-30'):
            return http.get(f'/api/reports/resumo?tipo={tipo}&inicio={inicio}&fim={fim}',
                            headers=cab)

        # --- GERAL: saldo anterior + movimento do periodo ---------------------
        r = resumo('geral')
        assert r.status_code == 200, r.data
        d = r.get_json()
        v = valores(d)
        assert d['empresa'] == 'Fozesc Teste', d['empresa']
        assert v['Saldo anterior ao período'] == 1000.0, v
        assert v['Entradas no período'] == 800.0, v
        assert v['Saídas no período'] == -200.0, v       # abs() mesmo gravado negativo
        assert v['Resultado do período'] == 600.0, v
        assert v['Saldo final'] == 1600.0, v

        # gráfico temporal: junho tem o movimento, o resto do período fica em zero
        barras = next(g for g in d['graficos'] if g['titulo'] == 'Entradas e saídas no período')
        assert len(barras['labels']) == len(barras['series'][0]['dados']) == 30, barras['labels']
        assert round(sum(barras['series'][0]['dados']), 2) == 800.0, barras
        assert round(sum(barras['series'][1]['dados']), 2) == 200.0, barras

        # saldo por conta usa a MESMA classificação do Fluxo de Caixa (bank_key)
        contas = next(t for t in d['tabelas'] if t['titulo'] == 'Por conta')
        por_nome = {l[0]: l for l in contas['linhas']}
        assert por_nome['Banco do Brasil'][3] == 500.0, contas
        assert por_nome['Caixa Econômica'][3] == 300.0, contas
        assert por_nome['Dinheiro'][3] == 800.0, contas   # 1000 de aporte - 200 de saída

        categorias = next(g for g in d['graficos'] if g['titulo'] == 'Maiores saídas por categoria')
        assert categorias['labels'] == ['Bordero'] and categorias['series'][0]['dados'] == [200.0], categorias

        # --- LUCRO: recebido x gerado, sem o historico ------------------------
        d = resumo('lucro').get_json()
        v = valores(d)
        # so o cheque pago DENTRO do periodo (100). Nao entra o pago em agosto (80)
        # nem o historico fora_do_calculo (999).
        assert v['Juros de cheques recebidos no período'] == 100.0, v
        # juros gerado = cheques do bordero de junho que contam: 100+80+50+20+70 = 320
        assert v['Juros gerado em borderôs operados no período'] == 320.0, v
        assert v['IOF cobrado nos borderôs do período'] == 37.58, v
        # face = 1000+800+500+200+700 = 3200 (sem os 9999/8888 do historico)
        assert v['Valor de face operado no período'] == 3200.0, v
        assert v['Borderôs operados no período'] == 1, v

        linha = next(g for g in d['graficos'] if g['tipo'] == 'linha')
        assert [s['nome'] for s in linha['series']] == ['Recebido (cheque pago)', 'Gerado (borderô operado)'], linha
        assert round(sum(linha['series'][0]['dados']), 2) == 100.0, linha
        assert round(sum(linha['series'][1]['dados']), 2) == 320.0, linha
        top = next(t for t in d['tabelas'] if 'Maiores clientes' in t['titulo'])
        assert top['linhas'] == [['Cliente Teste', 320.0]], top

        # --- INADIMPLENCIA: so Atrasado/Devolvido/Juridico que contam ---------
        d = resumo('inadimplencia').get_json()
        v = valores(d)
        assert v['Juridico'] == 500.0, v          # sem os 8888 do historico
        assert v['Devolvido'] == 200.0, v
        assert v['Atrasado'] == 0.0, v
        assert v['Total em atraso'] == 700.0, v
        assert v['Quantidade de cheques'] == 2, v
        cheques = next(t for t in d['tabelas'] if t['titulo'] == 'Cheques do período')
        assert len(cheques['linhas']) == 2 and cheques['truncado'] is False, cheques
        # o total do rodapé tem que fechar com a lista impressa
        assert round(sum(l[5] for l in cheques['linhas']), 2) == v['Total em atraso'], cheques
        nomes = {l[2] for l in cheques['linhas']}
        assert nomes == {'Emitente Juridico', 'Emitente Devolvido'}, nomes
        assert 'Historico Juridico' not in nomes, nomes

        # pizza por situação e barras de maiores devedores, sem o histórico
        pizza = next(g for g in d['graficos'] if g['tipo'] == 'pizza')
        assert set(pizza['labels']) == {'Juridico', 'Devolvido'}, pizza
        assert round(sum(pizza['series'][0]['dados']), 2) == 700.0, pizza
        devedores = next(g for g in d['graficos'] if g['titulo'] == 'Maiores devedores')
        assert devedores['series'][0]['dados'] == [700.0], devedores
        assert {x['label']: x['valor'] for x in d['destaques']}['Cheques'] == 2, d['destaques']

        # --- periodo realmente filtra ----------------------------------------
        d = resumo('lucro', '2026-03-01', '2026-03-31').get_json()
        v = valores(d)
        assert v['Juros gerado em borderôs operados no período'] == 500.0, v
        assert v['Juros de cheques recebidos no período'] == 0.0, v

        d = resumo('inadimplencia', '2026-01-01', '2026-01-31').get_json()
        assert valores(d)['Total em atraso'] == 0.0, d
        assert d['tabelas'][0]['linhas'] == [], d['tabelas']
        assert d['graficos'] == [], 'sem dado não desenha gráfico vazio'

        # --- validacao de entrada --------------------------------------------
        assert resumo('hack').status_code == 400
        assert resumo('geral', 'ontem').status_code == 400
        assert resumo('geral', '2026-06-30', '2026-06-01').status_code == 400
        assert http.get('/api/reports/resumo?tipo=geral&inicio=2026-06-01&fim=2026-06-30'
                        ).status_code == 401, 'relatorio nao pode abrir sem login'

        print("OK: relatorio gerencial vem do banco - geral (saldo anterior, entradas, "
              "saidas com abs), lucro (recebido x gerado, IOF, face), inadimplencia "
              "(lista + total agregado + pizza), series temporais alinhadas com o "
              "periodo, fora_do_calculo ignorado, periodo filtrando, entrada validada "
              "e rota exigindo login.")
    finally:
        apaga_banco()


if __name__ == '__main__':
    main()
