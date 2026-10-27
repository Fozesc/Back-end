"""
Despesas recorrentes: debito automatico (lanca sozinho no vencimento, sem duplicar e
alcancando os dias em que o sistema ficou fechado), debito manual como o Receber (conta
escolhida na hora, partes, forma, pagamento parcial ate quitar), adiamento so da ocorrencia
atual, pausa, e o dia 31 que volta depois de fevereiro. A conta do cadastro e' so do automatico.
Banco SEPARADO (fozesc_teste_recorrentes), criado e apagado pelo teste.

Rode:  ./venv/bin/python test_recorrentes.py
"""
import os
import re
from datetime import date, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_recorrentes'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

SENHA = 'Senha#Teste123'
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
        from app.models.domain import CompanySettings, Recorrente, Transaction, User
        from app.services.recorrente_service import proximo_depois
        from werkzeug.security import generate_password_hash

        # ---------------------------------------------------------------- datas
        assert proximo_depois(date(2027, 1, 31), 'mensal', 31) == date(2027, 2, 28)
        assert proximo_depois(date(2027, 2, 28), 'mensal', 31) == date(2027, 3, 31), 'o dia 31 volta depois de fevereiro'
        assert proximo_depois(date(2027, 12, 10), 'mensal', 10) == date(2028, 1, 10)
        assert proximo_depois(date(2028, 2, 29), 'anual', 29) == date(2029, 2, 28)
        assert proximo_depois(date(2027, 1, 1), 'semanal', 1) == date(2027, 1, 8)

        app = create_app()
        _app, _db = app, db
        http = app.test_client()
        with app.app_context():
            db.session.add(User(name='Ana', email='ana@fozesc.com', password_hash=generate_password_hash(SENHA),
                                role='Operador', active=True))
            db.session.add(CompanySettings())
            db.session.commit()
        r = http.post('/api/auth/login', json={'email': 'ana@fozesc.com', 'password': SENHA})
        cab = {'Authorization': 'Bearer ' + r.get_json()['token']}
        hoje = date.today()
        URL = '/api/recorrentes'

        def post(url, dados, esperado=None):
            r = http.post(url, headers=cab, json=dados)
            if esperado:
                assert r.status_code == esperado, (url, dados, r.status_code, r.data)
            return r

        def caixa():
            with app.app_context():
                return [(t.description, t.amount, t.date, t.origin, t.category, t.recorrente_id)
                        for t in Transaction.query.order_by(Transaction.date, Transaction.id)]

        def gerar():
            return http.post(f'{URL}/gerar', headers=cab).get_json()['lancados']

        assert http.get(URL).status_code == 401

        # ------------------------------------------------------------ validacoes
        base = {'descricao': 'Aluguel', 'valor': 1500, 'conta': 'BB', 'proximo': hoje.isoformat()}
        for ruim in ({'descricao': ' '}, {'valor': 0}, {'valor': 'x'}, {'conta': 'Nubank'}, {'frequencia': 'diaria'},
                     {'tipo': 'outro'}, {'proximo': 'ontem'}, {'proximo': (hoje - timedelta(days=3)).isoformat()}):
            post(URL, {**base, **ruim}, 400)
        assert 'o que já venceu entraria no caixa' in post(URL, {**base, 'proximo': (hoje - timedelta(days=3)).isoformat()}).get_json()['error']

        # ---------------------------------------- debito automatico vencendo hoje
        aluguel = post(URL, {**base, 'obs': 'boleto chega por e-mail'}, 201).get_json()
        assert (aluguel['automatico'], aluguel['ativo'], aluguel['categoria'], aluguel['obs']) == (True, True, 'Despesas fixas', 'boleto chega por e-mail')
        lancados = gerar()
        assert [(l['descricao'], l['valor']) for l in lancados] == [('Aluguel (débito automático)', 1500)], lancados
        assert caixa() == [('Aluguel (débito automático)', -1500, hoje, 'BB', 'Despesas fixas', aluguel['id'])]
        assert gerar() == [] and len(caixa()) == 1, 'gerar de novo nao duplica'
        lista = {i['id']: i for i in http.get(URL, headers=cab).get_json()}
        assert lista[aluguel['id']]['proximo'] == proximo_depois(hoje, 'mensal', hoje.day).isoformat()
        assert lista[aluguel['id']]['ultimo'] == {'data': hoje.isoformat(), 'valor': 1500}
        saldo = http.get('/api/transactions/balances', headers=cab).get_json()['bruto']['bb_total']
        assert saldo == -1500, saldo

        # ------------- sistema fechado: alcanca as semanas atrasadas, cada uma no seu dia
        with app.app_context():
            semanal = Recorrente(descricao='Faxina', valor=100, tipo='saida', conta='Dinheiro', categoria='Limpeza',
                                 frequencia='semanal', proximo=hoje - timedelta(days=14), dia=1, automatico=True)
            db.session.add(semanal)
            db.session.commit()
            semanal_id = semanal.id
        assert [l['data'] for l in gerar()] == [(hoje - timedelta(days=d)).isoformat() for d in (14, 7, 0)]
        assert gerar() == []

        # ------------------------------ adiamento: so a ocorrencia atual anda
        sem_conta = {'descricao': 'Água', 'valor': 90, 'proximo': hoje.isoformat()}
        assert 'Escolha a conta' in post(URL, sem_conta, 400).get_json()['error'], 'automatico precisa da conta'
        luz = post(URL, {'descricao': 'Luz', 'valor': 230, 'proximo': hoje.isoformat(),
                         'automatico': False, 'categoria': 'Contas de consumo'}, 201).get_json()
        assert luz['conta'] is None, 'manual: a conta e escolhida na hora de lancar'
        assert http.put(f"{URL}/{luz['id']}", headers=cab, json={'automatico': True}).status_code == 400
        assert gerar() == [], 'manual nao lanca sozinho'
        post(f"{URL}/{luz['id']}/adiar", {'ate': hoje.isoformat()}, 400)        # precisa ser depois
        adiada = post(f"{URL}/{luz['id']}/adiar", {'ate': (hoje + timedelta(days=5)).isoformat()}, 200).get_json()
        assert (adiada['vencimento'], adiada['proximo']) == ((hoje + timedelta(days=5)).isoformat(), hoje.isoformat())
        assert post(f"{URL}/{luz['id']}/adiar", {'ate': None}, 200).get_json()['vencimento'] == hoje.isoformat(), 'desfaz'
        post(f"{URL}/{luz['id']}/adiar", {'ate': (hoje + timedelta(days=5)).isoformat()}, 200)

        # ---- debito manual como o Receber: conta da luz deste mes R$ 250, paga em duas partes e so uma parte
        outra_data = (hoje - timedelta(days=1)).isoformat()
        LANCAR = f"{URL}/{luz['id']}/lancar"
        parcial = post(LANCAR, {'valor_conta': 250, 'data': outra_data, 'partes': [
            {'conta': 'Dinheiro', 'forma': 'Dinheiro', 'valor': 100}, {'conta': 'BB', 'forma': 'PIX', 'valor': 50}]}, 201).get_json()
        assert (parcial['valor_atual'], parcial['pago_atual'], parcial['falta']) == (250, 150, 100), parcial
        assert (parcial['proximo'], parcial['vencimento']) == (hoje.isoformat(), (hoje + timedelta(days=5)).isoformat()), \
            'parcial nao anda o vencimento'
        assert [l for l in caixa() if l[0].startswith('Luz (parcial')] == [
            ('Luz (parcial · Parte 1/2)', -100, hoje - timedelta(days=1), 'Dinheiro', 'Contas de consumo', luz['id']),
            ('Luz (parcial · Parte 2/2 · PIX)', -50, hoje - timedelta(days=1), 'BB', 'Contas de consumo', luz['id'])]
        with app.app_context():
            grupos = {t.grupo_id for t in Transaction.query.filter(Transaction.description.like('Luz (parcial%'))}
        assert len(grupos) == 1 and None not in grupos, 'as partes ficam juntas no caixa'
        lista = {i['id']: i for i in http.get(URL, headers=cab).get_json()}
        assert lista[luz['id']]['ultimo'] == {'data': outra_data, 'valor': 150}, 'o ultimo soma as partes do dia'
        assert 'passa do que falta' in post(LANCAR, {'partes': [{'conta': 'Caixa', 'valor': 100.01}]}, 400).get_json()['error']
        post(LANCAR, {'valor_conta': 100, 'partes': [{'conta': 'Caixa', 'valor': 10}]}, 400)   # menor que o ja pago
        post(LANCAR, {'partes': [{'conta': 'Nubank', 'valor': 10}]}, 400)
        quitou = post(LANCAR, {'partes': [{'conta': 'Caixa', 'forma': 'Depósito', 'valor': 100}]}, 201).get_json()
        assert quitou['adiado_ate'] is None and quitou['proximo'] == proximo_depois(hoje, 'mensal', hoje.day).isoformat(), quitou
        assert (quitou['pago_atual'], quitou['falta'], quitou['ultimo']) == (0, 230, {'data': hoje.isoformat(), 'valor': 100})
        assert [l[:4] for l in caixa() if 'quitação' in l[0]] == [('Luz (quitação · Depósito)', -100, hoje, 'Caixa')]
        post(LANCAR, {'partes': [{'conta': 'BB', 'valor': 230}]}, 201)                     # o mes seguinte inteiro
        assert [l[1] for l in caixa() if l[0] == 'Luz (recorrente)'] == [-230]
        post(LANCAR, {'valor': -5, 'conta': 'Dinheiro'}, 201)   # forma curta; o sinal vem do tipo, nao do valor
        assert [l[1] for l in caixa() if l[0] == 'Luz (parcial)'] == [-5]

        # ------------------------------------------------ entrada recorrente
        aluguel_sala = post(URL, {'descricao': 'Aluguel da sala', 'valor': 800, 'tipo': 'entrada', 'conta': 'BB',
                                  'proximo': hoje.isoformat()}, 201).get_json()
        assert gerar()[0]['valor'] == 800 and caixa()[-1][:2] == ('Aluguel da sala (débito automático)', 800)

        # ------------------------------------------------ pausa
        r = http.put(f"{URL}/{semanal_id}", headers=cab, json={'ativo': False})
        assert r.status_code == 200 and r.get_json()['ativo'] is False
        post(f"{URL}/{semanal_id}/lancar", {}, 400)
        # retomar com o vencimento antigo lancaria a pausa toda: pede a data nova
        with app.app_context():
            db.session.get(Recorrente, semanal_id).proximo = hoje - timedelta(days=21)
            db.session.commit()
        assert http.put(f"{URL}/{semanal_id}", headers=cab, json={'ativo': True}).status_code == 400
        assert http.put(f"{URL}/{semanal_id}", headers=cab, json={'ativo': True, 'proximo': hoje.isoformat()}).status_code == 200
        # editar a descricao de uma automatica nao esbarra na regra da data
        assert http.put(f"{URL}/{aluguel['id']}", headers=cab, json={'descricao': 'Aluguel do escritório'}).status_code == 200

        # ------------------------------------------------ apagar: o caixa fica
        antes = len(caixa())
        assert http.delete(f"{URL}/{aluguel_sala['id']}", headers=cab).status_code == 204
        assert len(caixa()) == antes and caixa()[-1][-1] is None, 'o lancamento continua, sem o vinculo'
        assert http.delete(f"{URL}/{aluguel_sala['id']}", headers=cab).status_code == 404
        print('OK: despesas recorrentes (débito automático sem duplicar e alcançando os dias fechados, conta do cadastro '
              'só no automático, débito manual como o Receber: conta na hora, partes, forma e parcial até quitar; '
              'adiamento só da ocorrência atual, pausa, entrada, dia 31, apagar mantém o caixa).')
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
