import calendar
from datetime import date
from app import db
from app.models.domain import Transaction, Operation, Check
from sqlalchemy import func, case, extract, and_

LANCAMENTOS_POR_PAGINA = 50
MAX_LANCAMENTOS_POR_PAGINA = 100
# o que entra em 'Multas e Juros' no caixa, separado pelo comeco da descricao que o sistema grava
EXTRAS = (('prorrogacao', 'Juros de prorrogação%'), ('imposto', 'Imposto%'), ('multa', 'Multa%'))
LUCRO_VAZIO = {'juros_borderos': 0.0, 'qtd_borderos': 0, 'prorrogacao': 0.0, 'imposto': 0.0, 'multa': 0.0,
               'outros': 0.0, 'comissoes': 0.0, 'iof': 0.0, 'lucro_bruto': 0.0}

MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]


def bank_key(origin):
    """Classifica a conta pelo TEXTO de Transaction.origin - mesma regra do
    transaction_service.get_balances. E funcao de modulo porque o relatorio
    gerencial tambem precisa dela: uma regra so, para os numeros baterem."""
    o = (origin or "").upper()
    if 'BRASIL' in o or 'BB' in o:
        return 'BRASIL'
    if 'CAIXA' in o or 'CEF' in o:
        return 'CAIXA'
    return 'DINHEIRO'


NOME_CONTA = {
    'BRASIL': 'Banco do Brasil',
    'CAIXA': 'Caixa Econômica',
    'DINHEIRO': 'Dinheiro',
}


class HistoryService:
    """
    Histórico mensal do caixa — é apenas uma CONSULTA (não salva/fecha nada).
    Todos os valores são computados das mesmas tabelas/regras que o Fluxo de Caixa e o
    Dashboard já usam (Transaction + Operation), para que os números batam com o sistema.
    Nada aqui altera cálculo de juros.
    """

    def _label(self, year, month):
        return f"{MESES_PT[month]}/{year}"

    def _month_bounds(self, year, month):
        start = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        return start, date(year, month, last_day)

    def _bank_key(self, origin):
        return bank_key(origin)

    # ------------------------------------------------------------------ #
    # Lucro bruto por mes
    # ------------------------------------------------------------------ #
    def lucro_por_mes(self, inicio=None, fim=None):
        """Lucro bruto de cada mes ('AAAA-MM'), somado no banco (3 consultas para qualquer periodo):

          juros dos borderos feitos no mes (desagio, cheque por cheque, sem os fora do calculo)
        + juros de prorrogacao, imposto do Receber e multa de devolucao que entraram no caixa
          no mes (categoria 'Multas e Juros')
        - comissoes que sairam do caixa no mes (categoria 'Comissão': borderô e prorrogacao)

        IOF nao entra: e' do governo, so aparece como informacao."""
        meses = {}

        def mes(chave):
            return meses.setdefault(chave, dict(LUCRO_VAZIO))

        chave_op = func.to_char(Operation.operation_date, 'YYYY-MM')
        q = db.session.query(chave_op, func.coalesce(func.sum(Check.interest_amount), 0.0),
                             func.count(func.distinct(Operation.id)))\
            .join(Operation, Check.operation_id == Operation.id).filter(Check.fora_do_calculo.is_(False))
        if inicio:
            q = q.filter(Operation.operation_date >= inicio)
        if fim:
            q = q.filter(Operation.operation_date <= fim)
        for chave, juros, qtd in q.group_by(chave_op):
            mes(chave).update(juros_borderos=float(juros or 0), qtd_borderos=int(qtd or 0))

        ops_com_cheque = db.session.query(Check.operation_id).filter(Check.fora_do_calculo.is_(False)).distinct()
        q = db.session.query(chave_op, func.coalesce(func.sum(Operation.iof_amount), 0.0))\
            .filter(Operation.id.in_(ops_com_cheque))
        if inicio:
            q = q.filter(Operation.operation_date >= inicio)
        if fim:
            q = q.filter(Operation.operation_date <= fim)
        for chave, iof in q.group_by(chave_op):
            mes(chave)['iof'] = float(iof or 0)

        chave_tx = func.to_char(Transaction.date, 'YYYY-MM')
        tipo = case(*[(Transaction.description.like(padrao), nome) for nome, padrao in EXTRAS], else_='outros')
        tipo = case((Transaction.category == 'Comissão', 'comissoes'), else_=tipo)
        q = db.session.query(chave_tx, tipo, func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0))\
            .filter(db.or_(and_(Transaction.category == 'Multas e Juros', Transaction.type == 'entrada'),
                           and_(Transaction.category == 'Comissão', Transaction.type != 'entrada')))
        if inicio:
            q = q.filter(Transaction.date >= inicio)
        if fim:
            q = q.filter(Transaction.date <= fim)
        for chave, nome, valor in q.group_by(chave_tx, tipo):
            mes(chave)[nome] += float(valor or 0)

        for m in meses.values():
            for campo in m:
                if campo != 'qtd_borderos':
                    m[campo] = round(m[campo], 2)
            m['lucro_bruto'] = round(m['juros_borderos'] + m['prorrogacao'] + m['imposto'] + m['multa']
                                     + m['outros'] - m['comissoes'], 2)
        return meses

    def lucro_historico(self):
        """Todos os meses, do primeiro ao ultimo com movimento (mes sem nada aparece com zero,
        para o grafico nao pular meses)."""
        meses = self.lucro_por_mes()
        if not meses:
            return []
        ano, mes = map(int, min(meses).split('-'))
        ultimo = max(meses)
        saida = []
        while f"{ano:04d}-{mes:02d}" <= ultimo:
            chave = f"{ano:04d}-{mes:02d}"
            saida.append({'year': ano, 'month': mes, 'label': self._label(ano, mes),
                          **meses.get(chave, LUCRO_VAZIO)})
            ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
        return saida

    # ------------------------------------------------------------------ #
    # Resumo do mês (usado na lista de meses e no detalhe)
    # ------------------------------------------------------------------ #
    def compute_summary(self, year, month):
        year, month = int(year), int(month)
        start, end = self._month_bounds(year, month)

        entradas = db.session.query(
            func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0)
        ).filter(Transaction.type == 'entrada',
                 Transaction.date >= start, Transaction.date <= end).scalar() or 0.0

        saidas = db.session.query(
            func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0)
        ).filter(Transaction.type != 'entrada',
                 Transaction.date >= start, Transaction.date <= end).scalar() or 0.0

        # Saldo do caixa acumulado ATÉ o fim do mês (todas as movimentações)
        saldo_acumulado = db.session.query(
            func.coalesce(func.sum(
                case((Transaction.type == 'entrada', func.abs(Transaction.amount)),
                     else_=-func.abs(Transaction.amount))
            ), 0.0)
        ).filter(Transaction.date <= end).scalar() or 0.0

        # Lucro/total operado saem da soma dos CHEQUES que contam (fora_do_calculo=False),
        # nao mais dos totais gravados no bordero. Motivo: o historico da planilha antiga
        # (cheques ja pagos de 2021-2026) precisa ficar fora do Historico Mensal.
        # Conferido no banco: a soma por cheque bate exatamente com o total do bordero.
        lucro = db.session.query(
            func.coalesce(func.sum(Check.interest_amount), 0.0)
        ).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.operation_date >= start, Operation.operation_date <= end,
                 Check.fora_do_calculo.is_(False)).scalar() or 0.0

        # valor de face de quando operou: prorrogacao/recebimento parcial mudam o
        # `amount` de hoje, nao o que foi operado naquele mes
        total_operado = db.session.query(
            func.coalesce(func.sum(func.coalesce(Check.original_amount, Check.amount)), 0.0)
        ).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.operation_date >= start, Operation.operation_date <= end,
                 Check.fora_do_calculo.is_(False)).scalar() or 0.0

        qtd_operacoes = db.session.query(
            func.count(func.distinct(Operation.id))
        ).join(Check, Check.operation_id == Operation.id)\
         .filter(Operation.operation_date >= start, Operation.operation_date <= end,
                 Check.fora_do_calculo.is_(False)).scalar() or 0

        entradas = round(float(entradas), 2)
        saidas = round(float(saidas), 2)

        return {
            'year': year,
            'month': month,
            'label': self._label(year, month),
            'total_entradas': entradas,
            'total_saidas': saidas,
            'resultado': round(entradas - saidas, 2),
            'lucro': round(float(lucro), 2),
            'total_operado': round(float(total_operado), 2),
            'saldo_acumulado': round(float(saldo_acumulado), 2),
            'qtd_operacoes': int(qtd_operacoes),
        }

    # ------------------------------------------------------------------ #
    # Detalhe do mês: saldo por banco + crescimento/perda + lançamentos
    # ------------------------------------------------------------------ #
    def compute_month_detail(self, year, month, page=1, per_page=LANCAMENTOS_POR_PAGINA):
        year, month = int(year), int(month)
        start, end = self._month_bounds(year, month)

        resumo = self.compute_summary(year, month)
        lucro = self.lucro_por_mes(start, end).get(f"{year:04d}-{month:02d}", LUCRO_VAZIO)

        # Saldo por banco acumulado até o fim do mês (espelha get_balances, com data <= fim)
        acc_rows = db.session.query(
            Transaction.origin,
            func.sum(case((Transaction.type == 'entrada', func.abs(Transaction.amount)),
                          else_=-func.abs(Transaction.amount)))
        ).filter(Transaction.date <= end).group_by(Transaction.origin).all()

        # Movimentação por banco DENTRO do mês (crescimento = entradas / perda = saídas)
        mov_rows = db.session.query(
            Transaction.origin, Transaction.type,
            func.sum(func.abs(Transaction.amount))
        ).filter(Transaction.date >= start, Transaction.date <= end
                 ).group_by(Transaction.origin, Transaction.type).all()

        bancos = {
            'BRASIL':   {'nome': 'Banco do Brasil', 'saldo': 0.0, 'entradas': 0.0, 'saidas': 0.0},
            'CAIXA':    {'nome': 'Caixa Econômica',  'saldo': 0.0, 'entradas': 0.0, 'saidas': 0.0},
            'DINHEIRO': {'nome': 'Dinheiro',         'saldo': 0.0, 'entradas': 0.0, 'saidas': 0.0},
        }
        for origin, val in acc_rows:
            bancos[self._bank_key(origin)]['saldo'] += float(val or 0)
        for origin, tipo, val in mov_rows:
            key = self._bank_key(origin)
            if tipo == 'entrada':
                bancos[key]['entradas'] += float(val or 0)
            else:
                bancos[key]['saidas'] += float(val or 0)

        bancos_list = []
        for key in ['BRASIL', 'CAIXA', 'DINHEIRO']:
            b = bancos[key]
            bancos_list.append({
                'key': key,
                'nome': b['nome'],
                'saldo': round(b['saldo'], 2),
                'entradas': round(b['entradas'], 2),  # crescimento no mês
                'saidas': round(b['saidas'], 2),      # perda no mês
                'resultado': round(b['entradas'] - b['saidas'], 2),
            })

        # Lançamentos do mês (o extrato)
        # linha so informativa (total do borderô, juros sem a comissao) fica fora: nao e' dinheiro
        pagina = Transaction.query.filter(
            Transaction.date >= start, Transaction.date <= end, Transaction.valor_informativo.is_(None)
        ).order_by(Transaction.date.desc(), Transaction.id.desc()).paginate(
            page=page, per_page=per_page, max_per_page=MAX_LANCAMENTOS_POR_PAGINA, error_out=False)
        lancs = pagina.items

        lancamentos = [{
            'id': t.id,
            'data': t.date.strftime('%Y-%m-%d') if t.date else None,
            'descricao': t.description,
            'valor': t.amount,
            'tipo': t.type,
            'origem': t.origin,
            'categoria': t.category,
        } for t in lancs]

        return {
            'year': year,
            'month': month,
            'label': self._label(year, month),
            'resumo': resumo,
            'bancos': bancos_list,
            'lucro_bruto': lucro,
            'lancamentos': lancamentos,
            'lancamentos_total': pagina.total,
            'lancamentos_paginas': pagina.pages,
            'lancamentos_pagina': pagina.page,
            'lancamentos_por_pagina': pagina.per_page,
        }

    # ------------------------------------------------------------------ #
    # Lista de meses com atividade (para o seletor)
    # ------------------------------------------------------------------ #
    def get_available_months(self):
        meses = set()
        q_tx = db.session.query(
            extract('year', Transaction.date), extract('month', Transaction.date)
        ).filter(Transaction.date.isnot(None)).distinct().all()
        # So meses com bordero que tem cheque contando. Sem isto, os 4.113 borderos
        # importados abririam ~60 meses de historico vazio no seletor (inclusive o
        # "Setembro/1902" de uma data digitada errada na planilha).
        q_op = db.session.query(
            extract('year', Operation.operation_date), extract('month', Operation.operation_date)
        ).join(Check, Check.operation_id == Operation.id)\
         .filter(Operation.operation_date.isnot(None),
                 Check.fora_do_calculo.is_(False)).distinct().all()

        for y, m in list(q_tx) + list(q_op):
            if y and m:
                meses.add((int(y), int(m)))

        # so o nome do mes: o resumo vem no detalhe do mes escolhido. Antes calculava o resumo de
        # TODOS os meses (6 consultas cada) so para montar o seletor.
        return [{'year': y, 'month': m, 'label': self._label(y, m)} for (y, m) in sorted(meses, reverse=True)]
