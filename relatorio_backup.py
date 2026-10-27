"""Relatorio geral do periodo em PDF, para o e-mail do backup (gerar_backup.py).

Os numeros sao os do botao "Extrato" do Historico Mensal (ReportService.extrato_periodo);
o desenho segue o cabecalho padrao dos PDFs da tela (Front-end/src/utils/pdfBase.js).
Roda no container de backup, sem o navegador, por isso e' Python (fpdf2) e nao o jsPDF.
"""
import os
from urllib.parse import quote_plus

from fpdf import FPDF, FontFace
from fpdf.enums import EncryptionMethod, TableBordersLayout, TableCellFillMode

VERDE, VERMELHO, LARANJA, AZUL, ROXO = (5, 150, 105), (220, 38, 38), (234, 88, 12), (37, 99, 235), (126, 34, 206)
ESCURO = (15, 23, 42)
COR_SITUACAO = {'Atrasado': VERMELHO, 'Devolvido': LARANJA, 'Juridico': ROXO, 'Aguardando': AZUL}
NOME_SITUACAO = {'Juridico': 'Jurídico'}
MESES = ['', 'janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro',
         'outubro', 'novembro', 'dezembro']
# a fonte padrao do PDF so tem o alfabeto latino: o resto vira um parecido
TROCAS = str.maketrans({'—': '-', '–': '-', '−': '-', '…': '...', '“': '"', '”': '"', '‘': "'", '’': "'"})


def url_do_banco():
    u = os.environ
    return (u.get('DATABASE_URL') or
            f"postgresql://{u['POSTGRES_USER']}:{quote_plus(u['POSTGRES_PASSWORD'])}@"
            f"{u.get('POSTGRES_HOST', 'db_fozesc')}:{u.get('POSTGRES_PORT', '5432')}/{u['POSTGRES_DB']}")


def dados(inicio, fim):
    """O extrato do periodo, direto do banco, sem subir o sistema inteiro (o create_app
    mexe na estrutura do banco no boot, coisa que so o backend deve fazer)."""
    url = url_do_banco()
    os.environ.setdefault('DATABASE_URL', url)
    os.environ.setdefault('JWT_SECRET_KEY', 'so-para-o-relatorio-do-backup')
    from flask import Flask
    from app import db
    from app.services.report_service import ReportService
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=url, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        try:
            return ReportService().extrato_periodo(inicio.isoformat(), fim.isoformat())
        finally:
            db.session.remove()   # devolve a conexao antes de fechar todas
            db.engine.dispose()


def t(texto):
    return str('' if texto is None else texto).translate(TROCAS).encode('latin-1', 'replace').decode('latin-1')


def moeda(v):
    v = float(v or 0)
    s = f"{abs(v):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    return f"-R$ {s}" if v < -0.004 else f"R$ {s}"


def data_br(iso):
    return '/'.join(reversed(iso.split('-'))) if iso else ''


def titulo_do_periodo(inicio, fim):
    """'Relatório de outubro/2026' quando o periodo e' o mes; senao as datas."""
    if inicio.day == 1 and inicio.year == fim.year and inicio.month == fim.month:
        return f"Relatório de {MESES[inicio.month]}/{inicio.year}"
    return f"Relatório de {inicio:%d/%m} a {fim:%d/%m/%Y}"


class Pdf(FPDF):
    ESQ, LARGURA = 12, 273

    def __init__(self, empresa, cnpj, titulo, periodo, gerado_em):
        super().__init__(orientation='L', unit='mm', format='A4')
        self.empresa, self.cnpj, self.titulo, self.periodo, self.gerado_em = empresa, cnpj, titulo, periodo, gerado_em
        self.set_margins(self.ESQ, 14, self.ESQ)
        self.set_auto_page_break(True, 16)

    def texto(self, x, y, s, tam=9, negrito=False, cor=20, alinhar='L', largura=0):
        self.set_font('helvetica', 'B' if negrito else '', tam)
        self.set_text_color(*(cor if isinstance(cor, tuple) else (cor, cor, cor)))
        self.set_xy(x, y)
        self.cell(largura or self.LARGURA, 5, t(s), align=alinhar)

    def header(self):
        if self.page_no() == 1:
            self.set_fill_color(*ESCURO)
            self.rect(0, 0, 297, 3, 'F')
            self.texto(self.ESQ, 11, self.empresa, 16, True, ESCURO)
            self.texto(self.ESQ, 11, self.titulo, 13, True, ESCURO, 'R')
            self.texto(self.ESQ, 18, f"CNPJ {self.cnpj}" if self.cnpj else 'Gestão de recebíveis', 8.5, cor=100)
            self.texto(self.ESQ, 18, f"{self.periodo}  ·  gerado em {self.gerado_em}", 8.5, cor=100, alinhar='R')
            self.set_draw_color(*ESCURO)
            self.set_line_width(0.6)
            self.line(self.ESQ, 25, self.ESQ + self.LARGURA, 25)
            self.set_y(31)
        else:
            # folha solta diz de onde e'
            self.texto(self.ESQ, 7, self.empresa, 8.5, True, 60)
            self.texto(self.ESQ, 7, f"{self.titulo} · {self.periodo}", 8, cor=110, alinhar='R')
            self.set_draw_color(203, 213, 225)
            self.set_line_width(0.3)
            self.line(self.ESQ, 13, self.ESQ + self.LARGURA, 13)
            self.set_y(17)

    def footer(self):
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.2)
        self.line(self.ESQ, 196.5, self.ESQ + self.LARGURA, 196.5)
        self.texto(self.ESQ, 198, f"{self.empresa} · {self.titulo} · {self.periodo}", 7, cor=140)
        self.texto(self.ESQ, 198, f"Página {self.page_no()} de {{nb}}", 7, cor=140, alinhar='R')

    def caber(self, altura):
        if self.get_y() + altura > self.page_break_trigger:
            self.add_page()

    def secao(self, titulo, sub=''):
        self.caber(24)
        y = self.get_y()
        self.texto(self.ESQ, y, titulo, 10.5, True)
        if sub:
            self.texto(self.ESQ, y, sub, 7.5, cor=120, alinhar='R')
        self.set_y(y + 7)

    def quadros(self, lista):
        """[(rotulo, valor, sub, cor)] lado a lado."""
        vao, altura = 3, 17
        lc = (self.LARGURA - vao * (len(lista) - 1)) / len(lista)
        self.caber(altura + 4)
        y = self.get_y()
        for i, (rotulo, valor, sub, cor) in enumerate(lista):
            x = self.ESQ + i * (lc + vao)
            self.set_fill_color(248, 250, 252)
            self.set_draw_color(226, 232, 240)
            self.set_line_width(0.2)
            self.rect(x, y, lc, altura, 'DF', round_corners=True, corner_radius=1.5)
            self.texto(x + 3, y + 2, rotulo, 7.5, cor=100, largura=lc - 5)
            self.texto(x + 3, y + 6.8, valor, 11.5, True, cor, largura=lc - 5)
            self.texto(x + 3, y + 11.8, sub, 6.5, cor=120, largura=lc - 5)
        self.set_y(y + altura + 7)

    def tabela(self, colunas, linhas, vazio, total=None):
        """colunas [(titulo, largura, 'L'|'R')]; linhas [[(texto, cor)...]]; lista vazia vira uma frase."""
        if not linhas:
            self.caber(8)
            self.texto(self.ESQ + 2, self.get_y(), vazio, 8.5, cor=120)
            self.set_y(self.get_y() + 9)
            return
        self.caber(17)
        cabeca = FontFace(emphasis='BOLD', color=(80, 80, 80), fill_color=(241, 245, 249), size_pt=7.5)
        self.set_font('helvetica', '', 8)
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.15)
        with self.table(width=sum(c[1] for c in colunas), align='LEFT', col_widths=[c[1] for c in colunas],
                        text_align=[c[2] for c in colunas], line_height=4.6, padding=(0.8, 1.6),
                        headings_style=cabeca, borders_layout=TableBordersLayout.HORIZONTAL_LINES,
                        cell_fill_color=(250, 250, 251), cell_fill_mode=TableCellFillMode.EVEN_ROWS) as tab:
            linha = tab.row()
            for c in colunas:
                linha.cell(t(c[0]))
            for valores in linhas:
                linha = tab.row()
                for texto, cor in valores:
                    linha.cell(t(texto), style=FontFace(color=cor if isinstance(cor, tuple) else (cor, cor, cor)))
            if total:
                linha = tab.row()
                for texto in total:
                    linha.cell(t(texto), style=FontFace(emphasis='BOLD', color=(20, 20, 20), fill_color=(226, 232, 240)))
        self.set_y(self.get_y() + 6)


def gerar(d, caminho, titulo, senha=None):
    """Grava o PDF do extrato `d` em `caminho`. Com `senha`, so abre com ela (AES-256)."""
    soma = lambda lista, campo: sum(i.get(campo) or 0 for i in lista)
    periodo = data_br(d['inicio']) if d['inicio'] == d['fim'] else f"{data_br(d['inicio'])} a {data_br(d['fim'])}"
    pdf = Pdf(d['empresa'], d['cnpj'], titulo, periodo, d['gerado_em'])
    pdf.add_page()
    caixa, borderos, receb, prorr, vencidos, vales = (d['caixa'], d['borderos'], d['recebimentos'],
                                                     d['prorrogacoes'], d['vencidos'], d['vales'])
    r = caixa['resumo']

    pdf.quadros([
        ('Saldo inicial', moeda(r['saldo_anterior']), f"em {data_br(d['inicio'])}", 20),
        ('Entradas', moeda(r['entradas']), 'no período', VERDE),
        ('Saídas', moeda(-r['saidas']), f"{r['qtd']} lançamento(s) no caixa", VERMELHO),
        ('Resultado', moeda(r['entradas'] - r['saidas']), 'entradas - saídas', VERDE if r['entradas'] >= r['saidas'] else VERMELHO),
        ('Saldo final', moeda(r['saldo_final']), f"em {data_br(d['fim'])}", VERMELHO if r['saldo_final'] < 0 else 20),
    ])
    pdf.quadros([
        ('Borderôs feitos', moeda(soma(borderos, 'face')), f"{len(borderos)} borderô(s) · {soma(borderos, 'titulos')} título(s)", AZUL),
        ('Lucro dos borderôs', moeda(soma(borderos, 'juros')), f"juros · comissão {moeda(soma(borderos, 'comissao'))}", VERDE),
        ('Recebido de títulos', moeda(soma(receb, 'valor')), f"{d['quitados']['qtd']} título(s) quitado(s)", VERDE),
        ('Prorrogações', moeda(soma(prorr, 'juros')), f"{len(prorr)} prorrogação(ões) · juros", ROXO),
        ('Vales', moeda(soma(vales['saidos'], 'valor')), f"saíram · {moeda(vales['em_aberto'])} em aberto hoje", LARANJA),
    ])

    pc = caixa['por_conta']
    pdf.secao('Contas', 'saldo de cada conta no início e no fim do período')
    pdf.tabela([('Conta', 73, 'L'), ('Saldo inicial', 50, 'R'), ('Entradas', 50, 'R'), ('Saídas', 50, 'R'), ('Saldo final', 50, 'R')],
               [[(c['conta'], 20), (moeda(c['saldo_anterior']), 20), (moeda(c['entradas']), VERDE),
                 (moeda(-c['saidas']), VERMELHO), (moeda(c['saldo_final']), VERMELHO if c['saldo_final'] < 0 else 20)] for c in pc],
               '', total=['Total', moeda(soma(pc, 'saldo_anterior')), moeda(soma(pc, 'entradas')),
                          moeda(-soma(pc, 'saidas')), moeda(soma(pc, 'saldo_final'))])

    for lado, nome, sinal, cor in (('entrada', 'De onde veio o dinheiro', 1, VERDE), ('saida', 'Para onde foi o dinheiro', -1, VERMELHO)):
        cats = caixa['categorias'][lado]
        pdf.secao(nome, 'por categoria')
        pdf.tabela([('Categoria', 173, 'L'), ('Lançamentos', 50, 'R'), ('Total', 50, 'R')],
                   [[(c['categoria'], 20), (c['qtd'], 90), (moeda(sinal * c['total']), cor)] for c in cats],
                   'Nada no período.')

    pdf.secao(f"Borderôs feitos ({len(borderos)})", 'valor de face, o que a empresa ganha e o que foi pago ao cliente')
    pdf.tabela([('Data', 22, 'L'), ('Borderô', 18, 'L'), ('Cliente', 73, 'L'), ('Títulos', 16, 'R'), ('Valor de face', 30, 'R'),
                ('Juros', 28, 'R'), ('IOF', 24, 'R'), ('Comissão', 26, 'R'), ('Pago ao cliente', 36, 'R')],
               [[(data_br(b['data']), 20), (f"#{b['id']}", 90), (b['cliente'], 20), (b['titulos'], 90), (moeda(b['face']), 20),
                 (moeda(b['juros']), VERDE), (moeda(b['iof']), 90), (moeda(b['comissao']) if b['comissao'] else '-', 90),
                 (moeda(b['liquido']), 20)] for b in borderos],
               'Nenhum borderô no período.',
               total=['Total', '', f"{len(borderos)} borderô(s)", soma(borderos, 'titulos'), moeda(soma(borderos, 'face')),
                      moeda(soma(borderos, 'juros')), moeda(soma(borderos, 'iof')), moeda(soma(borderos, 'comissao')),
                      moeda(soma(borderos, 'liquido'))])

    titulo_de = lambda i: ' · '.join(x for x in (i.get('emitente'), i.get('numero') and f"nº {i['numero']}") if x)
    pdf.secao(f"Dinheiro recebido de títulos ({len(receb)})", 'recebimentos, parciais, juros de prorrogação e multas')
    pdf.tabela([('Data', 22, 'L'), ('Cliente', 60, 'L'), ('Título', 66, 'L'), ('Tipo', 45, 'L'), ('Conta', 40, 'L'), ('Valor', 40, 'R')],
               [[(data_br(i['data']), 20), (i['cliente'], 20), (titulo_de(i), 70), (i['categoria'], 90), (i['conta'], 90),
                 (moeda(i['valor']), VERDE)] for i in receb],
               'Nenhum recebimento no período.', total=['Total', '', '', '', '', moeda(soma(receb, 'valor'))])

    pdf.secao(f"Prorrogações ({len(prorr)})", 'títulos que ganharam novo vencimento')
    pdf.tabela([('Data', 22, 'L'), ('Cliente', 60, 'L'), ('Título', 76, 'L'), ('Vencia', 26, 'L'), ('Novo vencimento', 32, 'L'),
                ('Dias', 17, 'R'), ('Juros', 40, 'R')],
               [[(data_br(p['data']), 20), (p['cliente'], 20), (titulo_de(p), 70), (data_br(p['de']), 90),
                 (data_br(p['para']), 20), (p['dias'], 90), (moeda(p['juros']), ROXO)] for p in prorr],
               'Nenhuma prorrogação no período.', total=['Total', '', '', '', '', '', moeda(soma(prorr, 'juros'))])

    movs = sorted([{**v, 'saiu': True} for v in vales['saidos']] + list(vales['pagos']), key=lambda v: v['data'])
    pdf.secao('Vales', f"em aberto hoje: {moeda(vales['em_aberto'])}")
    pdf.tabela([('Data', 22, 'L'), ('Movimento', 26, 'L'), ('Descrição', 125, 'L'), ('Conta', 35, 'L'), ('Situação', 25, 'L'), ('Valor', 40, 'R')],
               [[(data_br(v['data']), 20), ('Saiu' if v.get('saiu') else 'Pagamento', VERMELHO if v.get('saiu') else VERDE),
                 (v['descricao'], 20), (v['conta'], 90), (v.get('status', '') if v.get('saiu') else '', 90),
                 (moeda(-v['valor'] if v.get('saiu') else v['valor']), VERMELHO if v.get('saiu') else VERDE)] for v in movs],
               'Nenhum vale no período.')

    pdf.secao(f"Venceram no período e não foram pagos ({len(vencidos)})", 'situação de hoje')
    pdf.tabela([('Vencimento', 24, 'L'), ('Cliente', 72, 'L'), ('Título', 100, 'L'), ('Situação', 37, 'L'), ('Valor', 40, 'R')],
               [[(data_br(v['vencimento']), 20), (v['cliente'], 20), (titulo_de(v), 70),
                 (NOME_SITUACAO.get(v['situacao'], v['situacao']), COR_SITUACAO.get(v['situacao'], 20)), (moeda(v['valor']), 20)]
                for v in vencidos],
               'Nada venceu sem pagamento no período.', total=['Total', f"{len(vencidos)} título(s)", '', '', moeda(soma(vencidos, 'valor'))])

    cart = lambda s: d['carteira'].get(s) or {'qtd': 0, 'valor': 0}
    todos = d['carteira'].values()
    pdf.secao('Carteira hoje', 'o que falta receber, na data em que o PDF foi gerado')
    pdf.quadros([
        ('A receber', moeda(sum(c['valor'] for c in todos)), f"{sum(c['qtd'] for c in todos)} título(s) em aberto", 20),
        ('A vencer', moeda(cart('Aguardando')['valor']), f"{cart('Aguardando')['qtd']} título(s)", AZUL),
        ('Atrasados', moeda(cart('Atrasado')['valor']), f"{cart('Atrasado')['qtd']} título(s)", VERMELHO),
        ('Devolvidos', moeda(cart('Devolvido')['valor']), f"{cart('Devolvido')['qtd']} título(s)", LARANJA),
        ('Jurídico', moeda(cart('Juridico')['valor']), f"{cart('Juridico')['qtd']} título(s)", ROXO),
    ])

    linhas = caixa['linhas']
    pdf.secao(f"Extrato do caixa ({len(linhas)} lançamento(s))", 'o saldo de cada linha soma todas as contas até ali')
    pdf.tabela([('Data', 20, 'L'), ('Descrição', 99, 'L'), ('Categoria', 38, 'L'), ('Conta', 30, 'L'), ('Entrada', 28, 'R'),
                ('Saída', 28, 'R'), ('Saldo', 30, 'R')],
               [[(data_br(i['data']), 20), (i['descricao'], 20), (i['categoria'], 90), (i['conta'], 90),
                 (moeda(i['entrada']) if i['entrada'] is not None else '', VERDE),
                 (moeda(-i['saida']) if i['saida'] is not None else '', VERMELHO),
                 (moeda(i['saldo']), VERMELHO if i['saldo'] < 0 else 20)] for i in linhas],
               'Nenhum lançamento no período.')
    if caixa['cortado']:
        pdf.texto(pdf.ESQ, pdf.get_y(), f"Atenção: o extrato parou em {len(linhas)} de {caixa['total']} lançamentos.", 8.5, True, VERMELHO)

    if senha:
        pdf.set_encryption(owner_password=senha, user_password=senha, encryption_method=EncryptionMethod.AES_256)
    pdf.output(caminho)
    return caminho
