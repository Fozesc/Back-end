import pandas as pd
from app import db
from app.models.domain import Check, Operation, Client, Transaction
from datetime import datetime
import os

class ReportService:
    
    def _get_path(self):
        base_dir = os.path.abspath(os.path.dirname(__file__))
        root_dir = os.path.dirname(os.path.dirname(base_dir))
        target_folder = os.path.join(root_dir, "backups")
        if not os.path.exists(target_folder): os.makedirs(target_folder)
        return target_folder

    def _format_date(self, date_obj):
        """Helper para formatar data DD/MM/AAAA para o Excel"""
        if not date_obj:
            return ""
        return date_obj.strftime('%d/%m/%Y')

    def gerar_relatorio_customizado(self, tipo, start_date=None, end_date=None):
        folder = self._get_path()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Nome do arquivo
        prefix = "BACKUP_COMPLETO" if tipo == 'legacy' else f"Relatorio_{tipo.upper()}"
        filename = f"{prefix}_{timestamp}.xlsx"
        filepath = os.path.join(folder, filename)
        
        writer = pd.ExcelWriter(filepath, engine='openpyxl')
        has_data = False

        # ==============================================================================
        # 1. ABA DE CHEQUES (Presente em 'legacy', 'geral' e 'cheques')
        # ==============================================================================
        if tipo in ['legacy', 'geral', 'cheques']:
            # Query Completa
            query = db.session.query(Check, Operation, Client)\
                .select_from(Check)\
                .join(Operation)\
                .join(Client)
            
            if start_date: query = query.filter(Check.due_date >= start_date)
            if end_date: query = query.filter(Check.due_date <= end_date)
            
            data_cheques = []
            for c, op, cli in query.all():
                
                # Tradução de Status para o padrão da planilha antiga (se for legacy)
                if tipo == 'legacy':
                    status_str = 'Pendente'
                    if c.status == 'Pago': status_str = 'Cobrado'
                    elif c.status == 'Juridico': status_str = 'Jurídico'
                    elif c.status == 'Devolvido': status_str = 'Devolvido'
                    elif c.status == 'Atrasado': status_str = 'Pendente'
                else:
                    status_str = c.status # Usa o status real do sistema nos outros relatórios

                item = {
                    'ID': c.id,
                    'Dt Operação': self._format_date(op.operation_date),
                    'Vencimento': self._format_date(c.due_date),
                    'Data PGTO': self._format_date(c.payment_date),
                    'Cliente': cli.name,
                    'Banco': c.bank,
                    'Emitente': c.issuer_name,
                    'Nº Doc': c.number,
                    'Destino': c.destination_bank,
                    # Valores Numéricos (Sem R$ para permitir soma no Excel)
                    'Valor Bruto (v)': float(c.amount or 0),
                    'Juros': float(c.interest_amount or 0),
                    'Valor Líquido': float(c.net_amount or 0),
                    'Status': status_str,
                    'Observação': op.notes or "",
                    'Forma': '' # Campo legado vazio
                }
                
                # Se for LEGACY, renomeia as chaves para bater EXATAMENTE com o importador
                if tipo == 'legacy':
                    item['v'] = item.pop('Valor Bruto (v)')
                    item['cobrado'] = item.pop('Status')
                    item['observação'] = item.pop('Observação')
                    # Remove ID para não confundir importador antigo se não precisar
                    # Mas se quiser manter ID, deixe. O importador ignora colunas extras.
                
                data_cheques.append(item)
            
            if data_cheques:
                df = pd.DataFrame(data_cheques)
                sheet_name = "Listado de cheques" if tipo == 'legacy' else "Cheques Detalhados"
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                has_data = True

        # ==============================================================================
        # 2. ABA FLUXO DE CAIXA (Presente em 'geral' e 'fluxo')
        # ==============================================================================
        if tipo in ['geral', 'fluxo']:
            query = Transaction.query
            if start_date: query = query.filter(Transaction.date >= start_date)
            if end_date: query = query.filter(Transaction.date <= end_date)

            data_fluxo = []
            for t in query.all():
                data_fluxo.append({
                    'ID': t.id,
                    'Data': self._format_date(t.date),
                    'Descrição': t.description,
                    'Categoria': t.category,
                    'Origem/Conta': t.origin,
                    'Tipo': t.type.upper(),
                    'Entrada': float(t.amount) if t.type == 'entrada' else 0,
                    'Saída': float(t.amount) if t.type == 'saida' else 0,
                    'Valor Total': float(t.amount)
                })

            if data_fluxo:
                df = pd.DataFrame(data_fluxo)
                df.to_excel(writer, sheet_name='Fluxo de Caixa', index=False)
                has_data = True

        # ==============================================================================
        # 3. ABA CLIENTES (Presente em 'geral' e 'clientes')
        # ==============================================================================
        if tipo in ['geral', 'clientes']:
            data_clientes = []
            for cli in Client.query.all():
                # Conta quantos cheques esse cliente tem
                qtd_cheques = len(cli.operations) # Aproximado por operações
                
                data_clientes.append({
                    'ID': cli.id,
                    'Nome': cli.name,
                    'Telefone': cli.phone,
                    'Documento': cli.document if hasattr(cli, 'document') else '',
                    'Observações': cli.notes,
                    'Qtd Operações': qtd_cheques
                })
            
            if data_clientes:
                df = pd.DataFrame(data_clientes)
                df.to_excel(writer, sheet_name='Base Clientes', index=False)
                has_data = True

        if not has_data:
            writer.close()
            return None

        writer.close()
        return filepath