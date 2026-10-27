"""
E-mail do backup: a planilha sai com uma aba por assunto (titulos, clientes, vales...) e sem
auditoria nem senha de usuario; o relatorio PDF do periodo sai protegido pela senha; e o
"desde o ultimo e-mail" so anda quando o envio da certo.
Banco SEPARADO (fozesc_teste_backup_email), criado e apagado pelo teste. Nao manda e-mail:
o envio (enviar_backups.py) e' trocado por um falso.

Rode:  ./venv/bin/python test_backup_email.py
"""
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta

URL_REAL = os.getenv('DATABASE_URL') or ''
if not URL_REAL:
    import dotenv
    dotenv.load_dotenv()
    URL_REAL = os.environ['DATABASE_URL']

BANCO_TESTE = 'fozesc_teste_backup_email'
URL_TESTE = re.sub(r'/[^/]+$', f'/{BANCO_TESTE}', URL_REAL)
os.environ['DATABASE_URL'] = URL_TESTE
os.environ.setdefault('JWT_SECRET_KEY', 'teste')
for k in ('POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB'):
    os.environ.setdefault(k, 'nao-usado')   # o gerar_backup exige; a conexao vem da DATABASE_URL

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

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
        from app.models.domain import (AuditLog, Check, CheckExtension, Client, ClientNote, CompanySettings,
                                       Evento, Operation, Recorrente, Transaction, User, Vale)
        from openpyxl import load_workbook
        import gerar_backup as g
        import relatorio_backup

        app = create_app()
        _app, _db = app, db
        hoje = date.today()
        with app.app_context():
            db.session.add_all([CompanySettings(company_name='Fozesc Teste', cnpj='12.345.678/0001-90'),
                                User(name='Ana', email='ana@x.com', password_hash='HASH-QUE-NAO-PODE-SAIR', role='Admin'),
                                AuditLog(user_name='Ana', action='LOGIN', target='Auth', description='AUDITORIA-NAO-SAI')])
            maria = Client(name='Maria Júlia', credit_limit=5000)
            db.session.add(maria)
            db.session.flush()
            op = Operation(client_id=maria.id, operation_date=hoje - timedelta(days=3), total_face_value=1000,
                           total_interest=40, iof_amount=4, total_net_value=956)
            db.session.add(op)
            db.session.flush()
            atrasado = Check(operation_id=op.id, issuer_name='João “Zé” Silva', number='9', amount=600,
                             due_date=hoje - timedelta(days=2))
            db.session.add_all([atrasado, Check(operation_id=op.id, issuer_name='Ana', amount=400, due_date=hoje,
                                                status='Pago', payment_date=hoje, paid_amount=400)])
            db.session.flush()
            vale = Vale(descricao='Adiantamento — Pedro', valor=300, data=hoje - timedelta(days=1), conta='BB')
            db.session.add(vale)
            db.session.flush()
            db.session.add_all([
                Transaction(date=hoje - timedelta(days=3), amount=-956, type='saida', origin='BB', category='Compra de Ativos',
                            description=f'Pgto Borderô #{op.id}', operation_id=op.id),
                Transaction(date=hoje - timedelta(days=3), amount=0, valor_informativo=40, type='entrada', origin='BB',
                            category='Informativo', description='INFORMATIVO-NAO-SAI'),
                Transaction(date=hoje, amount=400, type='entrada', origin='Dinheiro', category='Recebimento de Cheque',
                            description='Recebimento Cheque #10'),
                Transaction(date=hoje - timedelta(days=1), amount=-300, type='saida', origin='BB', category='Vale',
                            description='Vale', vale_id=vale.id),
                Transaction(date=hoje, amount=100, type='entrada', origin='BB', category='Vale',
                            description='Pagamento parcial do vale', vale_id=vale.id),
                CheckExtension(check_id=atrasado.id, prorrogation_date=datetime.now(), old_due_date=hoje - timedelta(days=9),
                               new_due_date=hoje - timedelta(days=2), days_added=7, fee_amount=30),
                Recorrente(descricao='Aluguel', valor=1500, proximo=hoje + timedelta(days=5), dia=1),
                Evento(data=hoje, titulo='Ligar para a Maria', autor='Ana'),
                ClientNote(client_id=maria.id, autor='Ana', texto='Paga sempre em dia'),
            ])
            db.session.commit()

        # ------------------------------------------------------------ planilha
        pasta = tempfile.mkdtemp()
        g.exportar_para_xlsx(pasta, 'TESTE')
        livro = load_workbook(os.path.join(pasta, 'TESTE.xlsx'))
        assert livro.sheetnames == list(g.ABAS), livro.sheetnames
        assert not any('audit' in n.lower() or 'usu' in n.lower() for n in livro.sheetnames)
        tudo = ' '.join(str(c.value) for f in livro.worksheets for linha in f.iter_rows() for c in linha)
        for proibido in ('HASH-QUE-NAO-PODE-SAIR', 'AUDITORIA-NAO-SAI', 'INFORMATIVO-NAO-SAI'):
            assert proibido not in tudo, proibido
        aba = lambda n: [[c.value for c in linha] for linha in livro[n].iter_rows()]
        titulos = aba('Títulos')
        sit = titulos[0].index('Situação')
        assert sorted(l[sit] for l in titulos[1:]) == ['Atrasado', 'Pago'], 'Aguardando vencido aparece como Atrasado'
        clientes = aba('Clientes')
        assert clientes[1][clientes[0].index('Em aberto')] == 600
        vales = aba('Vales')
        assert (vales[1][vales[0].index('Pago')], vales[1][vales[0].index('Falta pagar')]) == (100, 200), vales
        assert len(aba('Caixa')) == 5 and len(aba('Prorrogações')) == 2 and len(aba('Despesas recorrentes')) == 2

        # ------------------------------------------------------------ relatorio PDF
        d = relatorio_backup.dados(hoje - timedelta(days=7), hoje)
        assert d['empresa'] == 'Fozesc Teste' and len(d['borderos']) == 1 and len(d['caixa']['linhas']) == 4
        assert relatorio_backup.titulo_do_periodo(date(2026, 10, 1), date(2026, 10, 30)) == 'Relatório de outubro/2026'
        assert relatorio_backup.titulo_do_periodo(date(2026, 10, 23), date(2026, 10, 30)) == 'Relatório de 23/10 a 30/10/2026'
        aberto = relatorio_backup.gerar(d, os.path.join(pasta, 'aberto.pdf'), 'Relatório')
        fechado = relatorio_backup.gerar(d, os.path.join(pasta, 'fechado.pdf'), 'Relatório', senha='senha-do-teste-123')
        assert open(aberto, 'rb').read().startswith(b'%PDF') and b'/Encrypt' not in open(aberto, 'rb').read()
        assert b'/Encrypt' in open(fechado, 'rb').read() and b'/AESV3' in open(fechado, 'rb').read(), 'AES-256 com senha'
        vazio = relatorio_backup.dados(hoje + timedelta(days=30), hoje + timedelta(days=31))
        relatorio_backup.gerar(vazio, os.path.join(pasta, 'vazio.pdf'), 'Relatório')   # periodo sem nada nao quebra

        # ------------------------------------------------ envio (o enviar_backups.py e' falso aqui)
        g.ARQUIVO_ULTIMO_EMAIL = os.path.join(pasta, 'ULTIMO_EMAIL.json')
        g.DIR_RELATORIOS = os.path.join(pasta, 'relatorios')
        chamadas, resposta = [], {'codigo': 0}
        def envio_falso(cmd, env=None, **kw):
            chamadas.append(env)
            return subprocess.CompletedProcess(cmd, resposta['codigo'], stdout='enviado (teste)', stderr='')
        g.subprocess.run = envio_falso

        os.environ.pop('BACKUP_EMAIL', None)
        assert g.enviar_copia('semanal', hoje) is False and not chamadas, 'sem BACKUP_EMAIL nao envia'
        os.environ.update(BACKUP_EMAIL='quem@recebe.com', BACKUP_SENHA='senha-do-teste-123')
        assert g.enviar_copia('semanal', hoje) is True
        env = chamadas[-1]
        assert os.path.isfile(env['BACKUP_RELATORIO']) and b'/Encrypt' in open(env['BACKUP_RELATORIO'], 'rb').read()
        assert env['BACKUP_ASSUNTO'] == f"relatório de {hoje - timedelta(days=7):%d/%m} a {hoje:%d/%m/%Y}", env['BACKUP_ASSUNTO']
        assert g.ultimo_envio() == hoje

        resposta['codigo'] = 1                  # o Gmail recusou: nao conta como enviado
        assert g.enviar_copia('semanal', hoje + timedelta(days=7)) is False
        assert g.ultimo_envio() == hoje, 'falhou: o proximo ainda vem desde o ultimo que chegou'
        resposta['codigo'] = 0
        g.enviar_copia('semanal', hoje + timedelta(days=14))
        assert chamadas[-1]['BACKUP_ASSUNTO'].startswith(f"relatório de {hoje:%d/%m} a "), chamadas[-1]['BACKUP_ASSUNTO']
        print('OK: e-mail do backup - planilha com uma aba por assunto (sem auditoria, sem senha de usuário, sem linha '
              'informativa), relatório PDF do período com senha AES-256, e "desde o último e-mail" só anda quando o '
              'envio dá certo.')
    finally:
        if _app is not None:
            with _app.app_context():
                _db.engine.dispose()
        sql_admin(f'DROP DATABASE IF EXISTS {BANCO_TESTE}')


if __name__ == '__main__':
    main()
