from app.models.domain import Client, Operation, Check
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import or_, func, case

class ClientService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            from flask_jwt_extended import get_jwt
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

    def get_by_id(self, id):
        return Client.query.get(id)

    def create(self, data):
        client = Client(
            name=data.get('name'),
            document=data.get('document'),
            phone=data.get('phone'),
            email=data.get('email'),
            address=data.get('address'),
            credit_limit=data.get('credit_limit', 0.0),
            standard_rate=data.get('standard_rate', 4.0),
            notes=data.get('notes')
        )
        db.session.add(client)
        db.session.commit()

        self.audit.log_action(
            self._get_current_user(), 
            'CREATE', 
            'Cliente', 
            f"Novo cliente cadastrado: {client.name} | CPF/CNPJ: {client.document}"
        )
        return client

    def update(self, id, data):
        client = self.get_by_id(id)
        if not client: return None
        
        old_name = client.name
        changes = []

        if 'name' in data and data['name'] != client.name:
            changes.append(f"Nome: {client.name} -> {data['name']}")
            client.name = data['name']
        if 'document' in data and data['document'] != client.document:
            changes.append(f"Doc: {client.document} -> {data['document']}")
            client.document = data['document']
        if 'phone' in data: client.phone = data['phone']
        if 'credit_limit' in data: 
            changes.append(f"Limite: {client.credit_limit} -> {data['credit_limit']}")
            client.credit_limit = data['credit_limit']
        if 'standard_rate' in data: client.standard_rate = data['standard_rate']
        if 'notes' in data: client.notes = data['notes']
        
        db.session.commit()

        if changes:
            self.audit.log_action(
                self._get_current_user(),
                'UPDATE',
                'Cliente',
                f"Cliente {old_name} alterado. Detalhes: {', '.join(changes)}"
            )
        return client

    def delete(self, id):
        client = self.get_by_id(id)
        if not client: return False
        
        if client.operations: 
            raise ValueError(f"Não é possível excluir. O cliente possui {len(client.operations)} operações registradas.")

        name = client.name
        db.session.delete(client)
        db.session.commit()

        self.audit.log_action(
            self._get_current_user(),
            'DELETE',
            'Cliente',
            f"Cliente excluído permanentemente: {name}"
        )
        return True

    def get_paginated(self, page, per_page, search=None, status_filter=None):
        query = Client.query

        if search:
            term = f"%{search}%"
            query = query.filter(
                or_(
                    Client.name.ilike(term),
                    Client.document.ilike(term),
                    Client.phone.ilike(term)
                )
            )

        query = query.order_by(Client.name.asc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        items = []
        for client in pagination.items:
            stats = self._calculate_client_stats(client.id)
            is_pending = stats['total_debt'] > 0 and (client.credit_limit > 0 and stats['total_debt'] > client.credit_limit)

            items.append({
                'id': client.id,
                'name': client.name,
                'document': client.document,
                'phone': client.phone,
                'credit_limit': client.credit_limit,
                'standard_rate': client.standard_rate,
                'notes': client.notes,
                'valor_em_aberto': stats['total_debt'],
                'cheques_ativos': stats['active_count'],
                'cheques_totais': stats['total_count'],
                'pendencia': is_pending
            })

        return {
            'items': items,
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page
        }

    def _calculate_client_stats(self, client_id):
        totals = db.session.query(
            func.count(Check.id).label('total_count'),
            func.sum(case((Check.status != 'Pago', 1), else_=0)).label('active_count'),
            func.sum(case((Check.status != 'Pago', Check.amount), else_=0)).label('total_debt')
        ).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.client_id == client_id)\
         .first()

        return {
            'total_count': totals.total_count or 0,
            'active_count': totals.active_count or 0,
            'total_debt': totals.total_debt or 0.0
        }