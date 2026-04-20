from app.models.domain import Transaction, CompanySettings
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import func, or_
from sqlalchemy import func, case, or_
import datetime 
from datetime import datetime, date 

class TransactionService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:

            from flask_jwt_extended import get_jwt
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

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
        from app.models.domain import CompanySettings, Transaction, Check
        from sqlalchemy import func, case
        
        settings = CompanySettings.query.first()
        
        # 1. Capital Social (Para controle de crescimento/porcentagem)
        capital_base = float(settings.capital_social or 0)

       # Dentro de get_balances no transaction_service.py


        movimentacoes = db.session.query(
            Transaction.origin,
            func.sum(
                case(
                    (Transaction.type == 'entrada', func.abs(Transaction.amount)),
                    else_=-func.abs(Transaction.amount)
                )
            )
        ).group_by(Transaction.origin).all()

        saldos_brutos = {'BRASIL': 0.0, 'CAIXA': 0.0, 'DINHEIRO': 0.0}

        for origem, valor in movimentacoes:
            orig_upper = (origem or "").upper()
            val_float = float(valor or 0)
            
            # Mapeamento para garantir que SISTEMA (BB) e BB caiam no mesmo lugar
            if 'BRASIL' in orig_upper or 'BB' in orig_upper:
                saldos_brutos['BRASIL'] += val_float
            elif 'CAIXA' in orig_upper or 'CEF' in orig_upper:
                saldos_brutos['CAIXA'] += val_float
            else:
                saldos_brutos['DINHEIRO'] += val_float

        # 3. Resultado formatado para o Front
        res_bruto = {
            'bb_total': round(saldos_brutos['BRASIL'], 2),
            'caixa_total': round(saldos_brutos['CAIXA'], 2),
            'dinheiro_total': round(saldos_brutos['DINHEIRO'], 2),
            'capital_total': capital_base
        }

        # 4. Cheques na rua (para o cálculo do disponível)
        cheques_na_rua = db.session.query(
            Check.bank, 
            func.sum(Check.amount)
        ).filter(Check.status.in_(['Aguardando', 'Atrasado'])).group_by(Check.bank).all()

        na_rua_map = {'BRASIL': 0.0, 'CAIXA': 0.0, 'DINHEIRO': 0.0}
        for banco, valor in cheques_na_rua:
            b_upper = (banco or "").upper()
            if 'BRASIL' in b_upper or 'BB' in b_upper:
                na_rua_map['BRASIL'] += float(valor)
            elif 'CAIXA' in b_upper or 'CEF' in b_upper:
                na_rua_map['CAIXA'] += float(valor)
            else:
                na_rua_map['DINHEIRO'] += float(valor)

        return {
            'bruto': res_bruto,
            'na_rua': na_rua_map,
            'capital_total': capital_base
        }

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
        self.audit.log_action(self._get_current_user(), 'CREATE', 'FluxoCaixa', 
                             f"Lançamento: {new_t.description} | R$ {new_t.amount} ({new_t.origin})")
        return self._serialize(new_t)

    def update(self, id, data):
        t = Transaction.query.get(id)
        if not t: return None
        
        # --- ADICIONE ESTAS DUAS LINHAS AQUI ---
        antiga_desc = t.description
        antigo_valor = t.amount
        # ---------------------------------------

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
        
        # Agora o log vai funcionar porque as variáveis existem!
        self.audit.log_action(self._get_current_user(), 'UPDATE', 'FluxoCaixa', 
                             f"Editou lançamento #{id}: {antiga_desc} (R$ {antigo_valor}) -> {t.description} (R$ {t.amount})")
        return self._serialize(t)
    
    def delete(self, id):
        t = Transaction.query.get(id)
        if not t: 
            return False
        
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
            'category': t.category
        }