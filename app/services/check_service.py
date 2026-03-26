from app.models.domain import Check, Operation, Client, Transaction, CheckExtension
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import or_, and_, desc, asc, func
from datetime import datetime, date
from flask import request
class CheckService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            verify_jwt_in_request(optional=True)
            return get_jwt_identity() or 'Sistema'
        except:
            return 'Sistema'

    def get_paginated(self, page, per_page, search=None, status=None, date_start=None, date_end=None, sort_by='due_date', sort_order='asc'):
        query = Check.query.join(Operation).join(Client)

        if search:
            term = f"%{search}%"
            filtros_busca = [
                Check.issuer_name.ilike(term),
                Client.name.ilike(term),
                Check.number.ilike(term),
                Check.bank.ilike(term)
            ]
            search_limpo = search.replace('#', '').strip()
            if search_limpo.isdigit():
                filtros_busca.append(Operation.id == int(search_limpo))
            query = query.filter(or_(*filtros_busca))

        if status:
            status_list = status.split(',')
            status_list = [s for s in status_list if s]
            if status_list and 'Todos' not in status_list:
            
                if 'Atrasado' in status_list:
                    hoje = date.today()
                   
                    condicao_atrasado = or_(
                        Check.status == 'Atrasado',
                        and_(Check.status == 'Aguardando', Check.due_date < hoje)
                    )
                    
                    outros_status = [s for s in status_list if s != 'Atrasado']
                    if outros_status:
                        query = query.filter(or_(Check.status.in_(outros_status), condicao_atrasado))
                    else:
                        query = query.filter(condicao_atrasado)
                else:
                    query = query.filter(Check.status.in_(status_list))

        if date_start:
            hoje = date.today()
           
            query = query.filter(
                or_(
                    Check.due_date >= date_start,
                    and_(Check.status == 'Aguardando', Check.due_date < hoje),
                    Check.status.in_(['Atrasado', 'Devolvido'])
                )
            )
            
        if date_end:
            query = query.filter(Check.due_date <= date_end)

        sort_column = Check.due_date 
        if sort_by == 'amount': sort_column = Check.amount
        elif sort_by == 'issuer_name': sort_column = Check.issuer_name
        elif sort_by == 'due_date': sort_column = Check.due_date
            
        if sort_order == 'desc':
            query = query.order_by(desc(sort_column))
        else:
            query = query.order_by(asc(sort_column))

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

     
        items_serializados = []
        hoje = date.today()
        for c in pagination.items:

            if c.status == 'Aguardando' and c.due_date < hoje:
                c.status = 'Atrasado'
            items_serializados.append(self._serialize_check(c))

        return {
            'items': items_serializados,
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page
        }

    def get_portfolio_total(self):
        total = db.session.query(func.sum(Check.amount)).filter(
            Check.status.in_(['Aguardando', 'Atrasado', 'Juridico'])
        ).scalar()
        return {'total_portfolio': total or 0.0}


    def update_status(self, id, new_status, payment_data=None):
        check = Check.query.get(id)
        if not check: return False
        
        old_status = check.status
        check.status = new_status
        
        # --- 1. LÓGICA DE PAGAMENTO ---
        if new_status == 'Pago':
            if payment_data and payment_data.get('amount'):
                amount_paid = float(payment_data.get('amount'))
            else:
                amount_paid = check.amount
                
            method = payment_data.get('method', 'Dinheiro') if payment_data else 'Dinheiro'
            
            check.payment_date = datetime.now().date()
            check.paid_amount = amount_paid
            check.payment_method = method

            desc_tx = f"Recebimento Cheque #{check.number or 'S/N'} - {check.issuer_name}"
            transacao = Transaction(
                date=datetime.now(),
                description=desc_tx[:200],
                amount=amount_paid, 
                type='entrada',
                origin=method, 
                category='Recebimento de Cheque',
                operation_id=check.operation_id
            )
            db.session.add(transacao)
        
        # --- 2. LÓGICA DE CHEQUE DEVOLVIDO ---
        elif new_status == 'Devolvido' and old_status != 'Devolvido':
            taxa_multa = 2.0
            
            # --- O PULO DO GATO: Intercepta a requisição HTTP direto do Vue.js ---
            try:
                # Puxa o JSON original que o frontend enviou, ignorando o controlador
                req_data = request.get_json(silent=True)
                if req_data and 'taxa_multa' in req_data:
                    taxa_multa = float(req_data['taxa_multa'])
            except Exception as e:
                print(f"Não conseguiu ler a taxa enviada, usando 2%. Erro: {e}")
            # ----------------------------------------------------------------------

            # Calcula a multa sobre o valor de face
            multa_calculada = float(check.amount) * (taxa_multa / 100.0)
            check.fine_amount = multa_calculada

            # Lança APENAS a multa como entrada no Caixa
            desc_tx = f"Multa Devolução Cheque #{check.number or 'S/N'} - {check.issuer_name} ({taxa_multa}%)"
            transacao = Transaction(
                date=datetime.now(),
                description=desc_tx[:200],
                amount=multa_calculada, 
                type='entrada',
                origin='Sistema', 
                category='Multas e Juros',
                operation_id=check.operation_id
            )
            db.session.add(transacao)
        
        # --- 3. LÓGICA DE ESTORNO ---
        elif old_status == 'Pago' and new_status != 'Pago':
            check.payment_date = None
            check.paid_amount = 0.0
            
        db.session.commit()

        self.audit.log_action(self._get_current_user(), 'UPDATE', 'Cheque', f"Status alterado: {old_status} -> {new_status}")
        return check


    def prorrogate_check(self, check_id, new_date_str, fee_amount, notes):
        check = Check.query.get(check_id)
        if not check: return False, "Título não encontrado"

        try:
            old_date = check.due_date
            new_date_obj = datetime.strptime(new_date_str, '%Y-%m-%d').date()

            if not check.original_due_date:
                check.original_due_date = old_date

            fee_amount_float = float(fee_amount) if fee_amount else 0.0

            extension = CheckExtension(
                check_id=check.id,
                old_due_date=old_date,
                new_due_date=new_date_obj,
                days_added=(new_date_obj - old_date).days,
                fee_amount=fee_amount_float,
                notes=notes,
                status='PAGO' 
            )
            db.session.add(extension)

            # --- LANÇA A TAXA DE PRORROGAÇÃO COMO ENTRADA NO CAIXA ---
            if fee_amount_float > 0:
                desc_tx = f"Taxa Prorrogação Cheque #{check.number or 'S/N'} - {check.issuer_name}"
                transacao = Transaction(
                    date=datetime.now(),
                    description=desc_tx[:200],
                    amount=fee_amount_float,
                    type='entrada',
                    origin='Sistema',
                    category='Multas e Juros',
                    operation_id=check.operation_id
                )
                db.session.add(transacao)

            check.due_date = new_date_obj
            check.status = 'Prorrogado' 
            
            db.session.commit()
            
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Cheque', f"Prorrogação Cheque #{check.number}: {old_date} -> {new_date_str}")
            return True, "Prorrogação realizada com sucesso"
        except Exception as e:
            db.session.rollback()
            return False, str(e)

    def delete(self, id):
        check = Check.query.get(id)
        if not check: return False
        db.session.delete(check)
        db.session.commit()
        return True

    def _serialize_check(self, c):
        client_name = c.operation.client.name if c.operation and c.operation.client else "N/A"
        data_entrada = c.operation.operation_date.strftime('%Y-%m-%d') if c.operation else None
        
        extensions_history = []
        if hasattr(c, 'extensions'):
            for ext in c.extensions:
                extensions_history.append({
                    'id': ext.id,
                    'data_simulacao': ext.prorrogation_date.strftime('%Y-%m-%d'),
                    'de': ext.old_due_date.strftime('%Y-%m-%d'),
                    'para': ext.new_due_date.strftime('%Y-%m-%d'),
                    'dias': ext.days_added,
                    'taxa': ext.fee_amount
                })

        return {
            'id': c.id,
            'operation_id': c.operation_id,
            'type': c.type or 'Cheque', 
            
            'vencimento': c.due_date.strftime('%Y-%m-%d'),
            'vencimento_original': c.original_due_date.strftime('%Y-%m-%d') if c.original_due_date else None,
            'data_entrada': data_entrada,
            'data_pagamento': c.payment_date.strftime('%Y-%m-%d') if c.payment_date else None,
            
            'valor_bruto': c.amount,
            'valor_pago': c.paid_amount, 
            'valor_liquido': c.net_amount,
            'juros': c.interest_amount,
            
            'cliente': client_name,
            'emitente': c.issuer_name,
            'banco': c.bank,
            'num_doc': c.number,
            'status': c.status,
            'destino': c.destination_bank,
            'observacao': c.operation.notes if c.operation else '',
            
            'historico_prorrogacao': extensions_history
        }