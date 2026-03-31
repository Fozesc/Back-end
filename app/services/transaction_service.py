from app.models.domain import Transaction, CompanySettings
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import func, or_
from datetime import datetime, date

class TransactionService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            verify_jwt_in_request(optional=True)
            return get_jwt_identity() or 'Sistema'
        except:
            return 'Sistema'

    # --- NOVA FUNÇÃO: SALVAR SALDOS INICIAIS ---
    def update_initial_balances(self, data):
        """
        Esta função grava na tabela CompanySettings os valores que servirão 
        de base para todo o cálculo do fluxo de caixa.
        """
        try:
            settings = CompanySettings.query.first()
            if not settings:
                settings = CompanySettings()
                db.session.add(settings)

            # Mapeia os campos vindos do Modal de Configuração
            if 'capital_social' in data:
                settings.capital_social = float(data.get('capital_social', 0))
            if 'saldo_inicial_bb' in data:
                settings.saldo_inicial_bb = float(data.get('saldo_inicial_bb', 0))
            if 'saldo_inicial_ce' in data:
                settings.saldo_inicial_ce = float(data.get('saldo_inicial_ce', 0))
            if 'saldo_inicial_caixa' in data:
                settings.saldo_inicial_caixa = float(data.get('saldo_inicial_caixa', 0))

            db.session.commit()
            
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Configuracao', "Atualizou saldos iniciais do caixa")
            return True
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao salvar saldos iniciais: {e}")
            return False

    def get_balances(self):
        # 1. Busca os pontos de partida (Saldos Iniciais)
        settings = CompanySettings.query.first()
        
        bb_total = settings.saldo_inicial_bb if settings else 0.0
        ce_total = settings.saldo_inicial_ce if settings else 0.0
        dinheiro_total = settings.saldo_inicial_caixa if settings else 0.0
        capital = settings.capital_social if settings else 0.0
        
        # 2. Soma toda a movimentação histórica
        transacoes = Transaction.query.all()

        for t in transacoes:
            valor = float(t.amount) if t.amount is not None else 0.0
            tipo = (t.type or "").lower().strip()
            origem = (t.origin or "").upper().strip()

            multiplicador = 1 if 'entrada' in tipo else -1
            valor_real = valor * multiplicador
            
            # Lógica de unificação: Se for BB ou Caixa, vai pra conta específica. 
            # Qualquer outra coisa (PIX, Dinheiro, etc) cai no monte do Dinheiro.
            if 'BRASIL' in origem or 'BB' in origem:
                bb_total += valor_real
            elif 'CAIXA' in origem or 'CEF' in origem:
                ce_total += valor_real
            else:
                dinheiro_total += valor_real

        return {
            'total': bb_total + ce_total + dinheiro_total,
            'bb': bb_total,
            'caixa': ce_total,
            'dinheiro': dinheiro_total, # Unificado com PIX visualmente no front
            'capital_investido': capital
        }

    # --- RESTANTE DAS FUNÇÕES (Mantidas originais para não estragar nada) ---
    
    def get_paginated(self, page, per_page, search=None, date_filter=None, type_filter=None):
        db.session.query(Transaction).filter(
            or_(Transaction.amount == None, Transaction.date == None)
        ).delete(synchronize_session=False)
        db.session.commit()

        query = Transaction.query.order_by(Transaction.date.desc(), Transaction.id.desc())
        if search: query = query.filter(Transaction.description.ilike(f"%{search}%"))
        if date_filter: query = query.filter(func.date(Transaction.date) == date_filter)
        if type_filter and type_filter != 'todos': query = query.filter(Transaction.type == type_filter)

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        entradas = sum(t.amount for t in pagination.items if t.type == 'entrada')
        saidas = sum(t.amount for t in pagination.items if t.type != 'entrada')

        return {
            'items': [self._serialize(t) for t in pagination.items],
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page,
            'summary': { 'entradas': entradas, 'saidas': saidas, 'resultado': entradas - saidas }
        }

    def create(self, data):
        val = data.get('amount') or data.get('valor')
        desc = data.get('description') or data.get('descricao') or 'Lançamento Manual'
        dt = data.get('date') or data.get('data') or datetime.now()
        if isinstance(dt, str): dt = datetime.strptime(dt[:10], '%Y-%m-%d').date()

        new_t = Transaction(
            date=dt, description=desc, amount=float(val or 0.0),
            type=data.get('type') or data.get('tipo') or 'saida',
            origin=data.get('origin') or data.get('origem') or 'Dinheiro',
            category=data.get('category') or 'Geral',
            operation_id=data.get('operation_id')
        )
        db.session.add(new_t)
        db.session.commit()
        return self._serialize(new_t)

    def update(self, id, data):
        t = Transaction.query.get(id)
        if not t: return None
        if 'date' in data or 'data' in data:
            dt = data.get('date') or data.get('data')
            t.date = datetime.strptime(dt[:10], '%Y-%m-%d').date() if isinstance(dt, str) else dt
        if 'description' in data: t.description = data['description']
        elif 'descricao' in data: t.description = data['descricao']
        val = data.get('amount') or data.get('valor')
        if val is not None: t.amount = float(val)
        if 'type' in data: t.type = data['type']
        elif 'tipo' in data: t.type = data['tipo']
        if 'origin' in data: t.origin = data['origin']
        elif 'origem' in data: t.origin = data['origem']
        db.session.commit()
        return self._serialize(t)
    
    def delete(self, id):
        t = Transaction.query.get(id)
        if not t: return False
        db.session.delete(t)
        db.session.commit()
        return True

    def _serialize(self, t):
        return {
            'id': t.id,
            'data': t.date.strftime('%Y-%m-%d') if t.date else None,
            'descricao': t.description,
            'valor': t.amount,
            'tipo': t.type,
            'origem': t.origin,
            'category': t.category
        }