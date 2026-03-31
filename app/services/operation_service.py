from datetime import datetime
import math
from app import db
from app.models.domain import Operation, Check, Transaction, Client
from flask_jwt_extended import get_jwt
from app.services.audit_service import AuditService
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

class OperationService:
    def __init__(self):
        self.audit = AuditService()
    
    def _calcular_arredondamento_js(self, valor):
        return int((valor * 100) + 0.5) / 100.0

    def calculate_check_values(self, valor_face, data_base, data_vencimento, taxa_mensal, dias_flutuacao=0):
        if isinstance(data_base, str):
            data_base = datetime.strptime(data_base, '%Y-%m-%d').date()
        if isinstance(data_vencimento, str):
            data_vencimento = datetime.strptime(data_vencimento, '%Y-%m-%d').date()
            
        diff = (data_vencimento - data_base).days
        dias_totais = diff + int(dias_flutuacao)
        
        valor_face = float(valor_face)
        
        
        if dias_totais <= 0:
            return dias_totais, 0.0, valor_face
        
        taxa_decimal = float(taxa_mensal) / 100.0
        

        total_meses = dias_totais / 30.0
        fator = math.pow(1 + taxa_decimal, total_meses)
        
        valor_liquido_raw = valor_face / fator
        valor_liquido = self._calcular_arredondamento_js(valor_liquido_raw)
        valor_juros_final = valor_face - valor_liquido
        
        return dias_totais, valor_juros_final, valor_liquido

    from datetime import datetime
import math
from app import db
from app.models.domain import Operation, Check, Transaction, Client
from flask_jwt_extended import get_jwt
from app.services.audit_service import AuditService
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

class OperationService:
    def __init__(self):
        self.audit = AuditService()
    
    def _calcular_arredondamento_js(self, valor):
        return int((valor * 100) + 0.5) / 100.0

    def calculate_check_values(self, valor_face, data_base, data_vencimento, taxa_mensal, dias_flutuacao=0):
        if isinstance(data_base, str):
            data_base = datetime.strptime(data_base, '%Y-%m-%d').date()
        if isinstance(data_vencimento, str):
            data_vencimento = datetime.strptime(data_vencimento, '%Y-%m-%d').date()
            
        diff = (data_vencimento - data_base).days
        dias_totais = diff + int(dias_flutuacao)
        
        valor_face = float(valor_face)
        
        
        if dias_totais <= 0:
            return dias_totais, 0.0, valor_face
        
        taxa_decimal = float(taxa_mensal) / 100.0
        

        total_meses = dias_totais / 30.0
        fator = math.pow(1 + taxa_decimal, total_meses)
        
        valor_liquido_raw = valor_face / fator
        valor_liquido = self._calcular_arredondamento_js(valor_liquido_raw)
        valor_juros_final = valor_face - valor_liquido
        
        return dias_totais, valor_juros_final, valor_liquido

    def create_operation(self, data):
        try:
            client = Client.query.get(data['client_id'])
            if not client:
                raise ValueError("Cliente não encontrado")

            op_date = data.get('operation_date', datetime.now().date())
            if isinstance(op_date, str):
                op_date = datetime.strptime(op_date, '%Y-%m-%d').date()

            conta_origem = data.get('account_source', 'Dinheiro') 
            origem_sistema = f"Sistema ({conta_origem})"
            
            dias_comp = int(data.get('dias_compensacao', 0))

            new_operation = Operation(
                client_id=data['client_id'],
                client_name_snapshot=client.name,
                operation_date=op_date,
                monthly_rate=float(data['taxa_mensal']), 
                compensation_days=dias_comp,
                account_source=conta_origem,
                notes=data.get('notes'),
                total_face_value=0.0,
                total_interest=0.0,
                total_net_value=0.0
            )
            
            db.session.add(new_operation)
            db.session.flush()

            acumulado_face = 0.0
            acumulado_juros = 0.0
            acumulado_liquido = 0.0
            
            checks_audit_list = []

            for item in data['checks']:
                valor_face = float(item['valor'])
                vencimento = item['vencimento']
                
                dias, juros, liquido = self.calculate_check_values(
                    valor_face=valor_face,
                    data_base=new_operation.operation_date,
                    data_vencimento=vencimento,
                    taxa_mensal=new_operation.monthly_rate,
                    dias_flutuacao=new_operation.compensation_days
                )
                
                new_check = Check(
                    operation_id=new_operation.id,
                    bank=item.get('banco', ''),
                    number=item.get('num_doc', ''),
                    due_date=datetime.strptime(vencimento, '%Y-%m-%d').date() if isinstance(vencimento, str) else vencimento,
                    amount=valor_face,
                    interest_amount=juros, 
                    net_amount=liquido,    
                    days=dias,
                    status='Aguardando',
                    destination_bank='Carteira',
                    issuer_name=item.get('emitente', '')
                )
                
                db.session.add(new_check)
                
                acumulado_face += valor_face
                acumulado_juros += juros
                acumulado_liquido += liquido
                
                checks_audit_list.append(f"[{new_check.bank} R$ {new_check.amount:.2f}]")

            new_operation.total_face_value = acumulado_face
            new_operation.total_interest = acumulado_juros
            new_operation.total_net_value = acumulado_liquido

            transaction = Transaction(
                date=new_operation.operation_date,
                description=f"Pgto Borderô #{new_operation.id} - {client.name}",
                amount=acumulado_liquido * -1,
                type='saida',
                origin=origem_sistema, 
                category='Compra de Ativos',
                operation_id=new_operation.id
            )
            db.session.add(transaction)

            db.session.commit()

            try:
                claims = get_jwt()
                user_name = claims.get('name', 'Sistema')
            except:
                user_name = 'Sistema'

            resumo_cheques_str = ", ".join(checks_audit_list)
            detalhes = (
                f"Novo Borderô #{new_operation.id} criado.\n"
                f"Cliente: {client.name}\n"
                f"Taxa: {new_operation.monthly_rate}% | Juros Total: R$ {new_operation.total_interest:.2f}\n"
                f"Valor Líquido Entregue: R$ {new_operation.total_net_value:.2f}\n"
                f"Cheques ({len(data['checks'])}): {resumo_cheques_str}"
            )

            self.audit.log_action(user_name, 'CREATE', 'Borderô', detalhes)
                
            return new_operation

        except Exception as e:
            db.session.rollback()
            raise e
            
    def get_all(self):
        return Operation.query.all()

    def get_by_client(self, client_id):
    
        ops = Operation.query\
            .filter(Operation.client_id == client_id)\
            .options(joinedload(Operation.checks))\
            .order_by(desc(Operation.operation_date))\
            .all()
            
        return [self._serialize_with_checks(op) for op in ops]

    def _serialize_with_checks(self, op):
        return {
            'id': op.id,
            'client_id': op.client_id,
            'date': op.operation_date.strftime('%Y-%m-%d'),
            'total_face_value': op.total_face_value, 
            'total_net_value': op.total_net_value,
            'status': op.status,
            'notes': op.notes,
            'cheques': [{
                'id': c.id,
                'due_date': c.due_date.strftime('%Y-%m-%d'),
                'amount': c.amount,
                'status': c.status,
                'bank': c.bank,
                'number': c.number,
                'issuer_name': c.issuer_name
            } for c in op.checks]
        }
    
    def get_all(self):
        return Operation.query.all()

    def get_by_client(self, client_id):
    
        ops = Operation.query\
            .filter(Operation.client_id == client_id)\
            .options(joinedload(Operation.checks))\
            .order_by(desc(Operation.operation_date))\
            .all()
            
        return [self._serialize_with_checks(op) for op in ops]

    def _serialize_with_checks(self, op):
        return {
            'id': op.id,
            'client_id': op.client_id,
            'date': op.operation_date.strftime('%Y-%m-%d'),
            'total_face_value': op.total_face_value, 
            'total_net_value': op.total_net_value,
            'status': op.status,
            'notes': op.notes,
            'cheques': [{
                'id': c.id,
                'due_date': c.due_date.strftime('%Y-%m-%d'),
                'amount': c.amount,
                'status': c.status,
                'bank': c.bank,
                'number': c.number,
                'issuer_name': c.issuer_name
            } for c in op.checks]
        }