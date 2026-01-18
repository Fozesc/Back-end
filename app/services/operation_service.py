from app import db
from app.utils.sanitizer import sanitize_input
from app.models.domain import Operation, Check, Transaction
from datetime import datetime
import math

class OperationService:
    
    def _calcular_arredondamento_js(self, valor):
        return int((valor * 100) + 0.5) / 100.0

    def calculate_check(self, valor_face, data_base, data_vencimento, taxa, dias_comp):
        if isinstance(data_base, str):
            data_base = datetime.strptime(data_base, '%Y-%m-%d').date()
        if isinstance(data_vencimento, str):
            data_vencimento = datetime.strptime(data_vencimento, '%Y-%m-%d').date()
            
        diff = (data_vencimento - data_base).days
        dias_totais = diff + dias_comp
        
        if dias_totais <= 0:
            return 0, 0.0, float(valor_face)
        
        taxa_decimal = taxa / 100.0
        total_mes = dias_totais / 30.0
        fator = pow(1 + taxa_decimal, total_mes)
        
        valor_presente = float(valor_face)
        valor_futuro = valor_presente * fator
        
        juros_bruto = valor_futuro - valor_presente
        juros = self._calcular_arredondamento_js(juros_bruto)
        
        liquido = valor_presente - juros
        
        return dias_totais, juros, liquido

    def create_operation(self, data):
    
        clean_data = sanitize_input(data)

       
        new_operation = Operation(
            client_id=clean_data.get('client_id'),
            client_name_snapshot=clean_data['client_name_snapshot'],
            operation_date=clean_data['operation_date'],
            applied_rate=clean_data['taxa_mensal'],
            total_gross=0.0,
            total_interest=0.0,
            total_net=0.0
        )
        
        db.session.add(new_operation)
        db.session.flush() 

        total_gross = 0.0
        total_interest = 0.0
        total_net = 0.0


        for check_data in clean_data['checks']:
            valor_face = float(check_data['valor'])
            vencimento = check_data['vencimento']
            
           
            dias, juros, liquido = self.calculate_check(
                valor_face=valor_face,
                data_base=new_operation.operation_date,
                data_vencimento=vencimento,
                taxa=new_operation.applied_rate,
                dias_comp=clean_data['dias_compensacao']
            )
            
            new_check = Check(
                operation_id=new_operation.id,
                bank=check_data.get('banco', ''),
                number=check_data.get('num_doc', ''),
                due_date=vencimento,
                amount=valor_face,
                interest_amount=juros, 
                net_amount=liquido,    
                days=dias,
                status='Aguardando'
            )
            
            db.session.add(new_check)
            
            total_gross += valor_face
            total_interest += juros
            total_net += liquido

    
        new_operation.total_gross = total_gross
        new_operation.total_interest = total_interest
        new_operation.total_net = total_net


        transaction = Transaction(
            date=new_operation.operation_date,
            description=f"Borderô #{new_operation.id} - {new_operation.client_name_snapshot}",
            amount=total_net,
            type='saida',
            origin='Sistema (Borderô)',
            category='Compra de Cheques',
            operation_id=new_operation.id
        )
        db.session.add(transaction)

        
        db.session.commit()
        
    
        return new_operation