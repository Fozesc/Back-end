from app.models.domain import Transaction, CompanySettings
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import func, or_
from datetime import datetime

class TransactionService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            verify_jwt_in_request(optional=True)
            return get_jwt_identity() or 'Sistema'
        except:
            return 'Sistema'
    
    def get_paginated(self, page, per_page, search=None, date_filter=None, type_filter=None):
       
        db.session.query(Transaction).filter(
            or_(Transaction.amount == None, Transaction.date == None)
        ).delete(synchronize_session=False)
        db.session.commit()

        query = Transaction.query.order_by(Transaction.date.desc(), Transaction.id.desc())

        if search:
            query = query.filter(Transaction.description.ilike(f"%{search}%"))
        
        if date_filter:
            query = query.filter(func.date(Transaction.date) == date_filter)

        if type_filter and type_filter != 'todos':
            query = query.filter(Transaction.type == type_filter)

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        entradas = 0
        saidas = 0
        for item in pagination.items:
            val = item.amount or 0.0
            if item.type == 'entrada': entradas += val
            else: saidas += val

        return {
            'items': [self._serialize(t) for t in pagination.items],
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page,
            'summary': {
                'entradas': entradas,
                'saidas': saidas,
                'resultado': entradas - saidas
            }
        }

    def get_balances(self):
        from app.models.domain import CompanySettings, Transaction # Garantindo os imports
        settings = CompanySettings.query.first()
        
        bb_total = settings.saldo_inicial_bb if settings else 0.0
        ce_total = settings.saldo_inicial_ce if settings else 0.0
        dinheiro_total = settings.saldo_inicial_caixa if settings else 0.0
        pix_total = 0.0 # Nova conta para o PIX
        capital = settings.capital_social if settings else 0.0
        
        transacoes = Transaction.query.all()

        for t in transacoes:
            valor = float(t.amount) if t.amount is not None else 0.0
            tipo = (t.type or "").lower().strip()
            origem = (t.origin or "").upper().strip()

            multiplicador = 1 if 'entrada' in tipo else -1
            valor_real = valor * multiplicador
            
            # Separação exata das contas
            if 'BRASIL' in origem or 'BB' in origem:
                bb_total += valor_real
            elif 'CAIXA' in origem or 'CEF' in origem:
                ce_total += valor_real
            elif 'PIX' in origem:
                pix_total += valor_real
            else:
                dinheiro_total += valor_real # Se não for nenhum dos 3, é dinheiro físico

        return {
            'total': bb_total + ce_total + dinheiro_total + pix_total,
            'bb': bb_total,
            'caixa': ce_total,
            'pix': pix_total,
            'dinheiro': dinheiro_total,
            'capital_investido': capital
        }
    def create(self, data):
        val = data.get('amount')
        if val is None: val = data.get('valor')
        
        desc = data.get('description') or data.get('descricao') or 'Lançamento Manual'
        
        new_t = Transaction(
            date=data.get('date') or data.get('data') or datetime.now(),
            description=desc,
            amount=float(val or 0.0),
            type=data.get('type') or data.get('tipo') or 'saida',
            origin=data.get('origin') or data.get('origem') or 'Dinheiro',
            category=data.get('category') or 'Geral',
            operation_id=data.get('operation_id')
        )
        db.session.add(new_t)
        db.session.commit()
        

        self.audit.log_action(
            self._get_current_user(), 
            'CREATE', 
            'FluxoCaixa', 
            f"Criou lançamento: {desc} | R$ {new_t.amount}"
        )
        
        return self._serialize(new_t)

    def update(self, id, data):
        t = Transaction.query.get(id)
        if not t: return None
        
        antigo_valor = t.amount
        
        if 'date' in data: t.date = data['date']
        elif 'data' in data: t.date = data['data']

        if 'description' in data: t.description = data['description']
        elif 'descricao' in data: t.description = data['descricao']

        val = data.get('amount')
        if val is None: val = data.get('valor')
        if val is not None: t.amount = float(val)

        if 'type' in data: t.type = data['type']
        elif 'tipo' in data: t.type = data['tipo']

        if 'origin' in data: t.origin = data['origin']
        elif 'origem' in data: t.origin = data['origem']
        
        db.session.commit()

       
        self.audit.log_action(
            self._get_current_user(), 
            'UPDATE', 
            'FluxoCaixa', 
            f"Editou lançamento #{id}: De R$ {antigo_valor} para R$ {t.amount}"
        )

        return self._serialize(t)
    
    def delete(self, id):
        t = Transaction.query.get(id)
        if not t: return False
        
        info = f"{t.description} (R$ {t.amount})"
        db.session.delete(t)
        db.session.commit()
        
     
        self.audit.log_action(
            self._get_current_user(), 
            'DELETE', 
            'FluxoCaixa', 
            f"Apagou lançamento: {info}"
        )
        
        return True

    def _serialize(self, t):
        return {
            'id': t.id,
            'data': t.date.strftime('%Y-%m-%d') if t.date else None,
            'descricao': t.description,
            'valor': t.amount,
            'tipo': t.type,
            'origem': t.origin,
            'category': t.category,
            'created_at': 'Hoje' 
        }