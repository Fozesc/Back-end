from app import db
from app.models.domain import Check, Transaction, CompanySettings, Operation
from sqlalchemy import func, case
from datetime import datetime, timedelta

class DashboardService:
    def get_dashboard_data(self, period='meses'):
     
        settings = CompanySettings.query.first()
        capital = settings.capital_social if settings else 0.0

        # FORA_DO_CALCULO: cheque marcado como historico (os pagos que vieram da
        # planilha antiga) nao entra em nenhum numero daqui. E' o que faz o lucro
        # acumulado "comecar de agora" sem apagar nada do historico.
        #
        # LUCRO = juros de cheque PAGO (juros que entrou de verdade). Antes somava o
        # juros de TODO cheque, pago ou nao: com a planilha importada isso mostrava
        # R$ 215 mil de juros de cheque antigo ainda em aberto como se fosse lucro.
        # Decisao do Lucas (15/09/2026). Os cheques em aberto continuam aparecendo
        # em Carteira e Inadimplencia - so nao contam como lucro antes de receber.
        lucro = db.session.query(func.sum(Check.interest_amount)).filter(
            Check.fora_do_calculo.is_(False),
            Check.status == 'Pago'
        ).scalar() or 0.0
        # Carteira = títulos ativos "na rua" (a receber). Passa a incluir 'Prorrogado'
        # (renegociado, o cliente ainda deve) além de 'Aguardando'. Antes o cheque
        # prorrogado sumia da carteira, subestimando o total a receber.
        carteira = db.session.query(func.sum(Check.amount)).filter(
            Check.status.in_(['Aguardando', 'Prorrogado']),
            Check.fora_do_calculo.is_(False)
        ).scalar() or 0.0
        inadimplencia = db.session.query(func.sum(Check.amount)).filter(
            Check.status.in_(['Atrasado', 'Devolvido', 'Juridico']),
            Check.fora_do_calculo.is_(False)
        ).scalar() or 0.0

        status_sums = db.session.query(
            Check.status, func.sum(Check.amount)
        ).filter(Check.fora_do_calculo.is_(False)).group_by(Check.status).all()
        
        status_map = {s: float(v or 0) for s, v in status_sums}
        pie_data = [
            status_map.get('Aguardando', 0),
            status_map.get('Pago', 0),      
            status_map.get('Atrasado', 0),  
            status_map.get('Devolvido', 0),
            status_map.get('Juridico', 0)    
        ]

      
        upcoming = Check.query.filter(
            Check.status.in_(['Aguardando', 'Prorrogado']),
            Check.due_date >= datetime.now().date(),
            Check.fora_do_calculo.is_(False)
        ).order_by(Check.due_date.asc()).limit(5).all()

        upcoming_data = [{
            'id': c.id, 
            'data': c.due_date.strftime('%Y-%m-%d'), 
            'cliente': c.operation.client.name, 
            'valor': c.amount,
            'banco': c.bank
        } for c in upcoming]

    
        evolution = self._get_evolution_data(period)

        return {
            'kpis': {
                'capital': capital, 'lucro': lucro,
                'carteira': carteira, 'inadimplencia': inadimplencia
            },
            'charts': {
                'pie_chart': pie_data,
                'evolution': evolution
            },
            'upcoming': upcoming_data
        }

    def _get_evolution_data(self, period):
        """Agrega dados de Lucro (Juros) e Caixa (Movimentação) por período"""
        

        end_date = datetime.now()
        if period == 'dias':
            start_date = end_date - timedelta(days=30)
            date_format = 'DD/MM' 
            trunc_type = 'day'    
        elif period == 'semanas':
            start_date = end_date - timedelta(weeks=12)
            date_format = 'Semana %W'
            trunc_type = 'week'
        else:
            start_date = end_date - timedelta(days=365)
            date_format = 'MM/YYYY'
            trunc_type = 'month'

      
        # Soma o juros CHEQUE POR CHEQUE (antes somava Operation.total_interest) para
        # poder tirar do grafico so os cheques marcados como historico. Conferido no
        # banco: sum(Check.interest_amount) == Operation.total_interest, diferenca 0,00
        # em todos os 4.119 borderos - o numero de quem conta nao muda.
        profit_query = db.session.query(
            func.to_char(Operation.operation_date, 'YYYY-MM-DD'),
            func.sum(Check.interest_amount)
        ).join(Check, Check.operation_id == Operation.id)\
         .filter(Operation.operation_date >= start_date,
                 Check.fora_do_calculo.is_(False))\
         .group_by(func.to_char(Operation.operation_date, 'YYYY-MM-DD'))\
         .all()

      
        cash_query = db.session.query(
            func.to_char(Transaction.date, 'YYYY-MM-DD'),
            func.sum(case((Transaction.type == 'entrada', Transaction.amount), else_=-Transaction.amount))
        ).filter(Transaction.date >= start_date)\
         .group_by(func.to_char(Transaction.date, 'YYYY-MM-DD'))\
         .all()

    
        data_map = {}
        
    
        for date_str, value in profit_query:
            d = datetime.strptime(date_str, '%Y-%m-%d')
            key = self._get_key(d, period)
            if key not in data_map: data_map[key] = {'profit': 0, 'cash': 0, 'sort': d}
            data_map[key]['profit'] += float(value or 0)

        for date_str, value in cash_query:
            d = datetime.strptime(date_str, '%Y-%m-%d')
            key = self._get_key(d, period)
            if key not in data_map: data_map[key] = {'profit': 0, 'cash': 0, 'sort': d}
            data_map[key]['cash'] += float(value or 0)


        sorted_keys = sorted(data_map.keys(), key=lambda k: data_map[k]['sort'])
        
        return {
            'labels': sorted_keys,
            'profit_data': [data_map[k]['profit'] for k in sorted_keys],
            'cash_data': [data_map[k]['cash'] for k in sorted_keys]
        }

    def _get_key(self, date_obj, period):
        if period == 'dias': return date_obj.strftime('%d/%m')
        if period == 'semanas': return f"Sem {date_obj.strftime('%W')}"
        return date_obj.strftime('%b/%y') # Ex: Jan/24