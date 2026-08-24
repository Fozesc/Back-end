import calendar
from datetime import date
from app import db
from app.models.domain import Transaction, Operation
from sqlalchemy import func, case, extract

MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]


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
        """Mesma classificação de conta usada em transaction_service.get_balances."""
        o = (origin or "").upper()
        if 'BRASIL' in o or 'BB' in o:
            return 'BRASIL'
        if 'CAIXA' in o or 'CEF' in o:
            return 'CAIXA'
        return 'DINHEIRO'

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

        lucro = db.session.query(
            func.coalesce(func.sum(Operation.total_interest), 0.0)
        ).filter(Operation.operation_date >= start,
                 Operation.operation_date <= end).scalar() or 0.0

        total_operado = db.session.query(
            func.coalesce(func.sum(Operation.total_face_value), 0.0)
        ).filter(Operation.operation_date >= start,
                 Operation.operation_date <= end).scalar() or 0.0

        qtd_operacoes = db.session.query(func.count(Operation.id)).filter(
            Operation.operation_date >= start, Operation.operation_date <= end
        ).scalar() or 0

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
    def compute_month_detail(self, year, month):
        year, month = int(year), int(month)
        start, end = self._month_bounds(year, month)

        resumo = self.compute_summary(year, month)

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
        lancs = Transaction.query.filter(
            Transaction.date >= start, Transaction.date <= end
        ).order_by(Transaction.date.desc(), Transaction.id.desc()).all()

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
            'lancamentos': lancamentos,
        }

    # ------------------------------------------------------------------ #
    # Lista de meses com atividade (para o seletor)
    # ------------------------------------------------------------------ #
    def get_available_months(self):
        meses = set()
        q_tx = db.session.query(
            extract('year', Transaction.date), extract('month', Transaction.date)
        ).filter(Transaction.date.isnot(None)).distinct().all()
        q_op = db.session.query(
            extract('year', Operation.operation_date), extract('month', Operation.operation_date)
        ).filter(Operation.operation_date.isnot(None)).distinct().all()

        for y, m in list(q_tx) + list(q_op):
            if y and m:
                meses.add((int(y), int(m)))

        ordenados = sorted(meses, reverse=True)
        return [self.compute_summary(y, m) for (y, m) in ordenados]
