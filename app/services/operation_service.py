from datetime import datetime
import math
import sys
from app import db
from app.models.domain import Operation, Check, Transaction, Client, CompanySettings
from flask_jwt_extended import get_jwt
from app.services.audit_service import AuditService
from sqlalchemy import asc, desc
from sqlalchemy.orm import joinedload

# Conta do borderô: a MESMA da tela (Front-end/src/utils/calculoBordero.js, calcularLinha).
# Antes o servidor recalculava cada cheque com outra formula (liquido = valor / fator) e
# gravava um juros menor que o da tela. Agora a tela e o servidor fazem a mesma conta.

def arredondar(valor):
    """Igual ao arredondar() da tela: Math.round((v + EPSILON) * 100) / 100 (meio centavo sobe)."""
    return math.floor((valor + sys.float_info.epsilon) * 100 + 0.5) / 100


def calcular_linha(valor_face, dias, taxa_mensal, iof_ativo=False, iof_base=0.0, iof_diario=0.0):
    """(juros, iof, liquido) de um titulo, na mesma ordem de contas da tela."""
    valor_face = float(valor_face or 0)
    if not valor_face or dias <= 0:
        return 0.0, 0.0, valor_face
    fator = math.pow(1 + float(taxa_mensal) / 100, dias / 30.0)
    juros = arredondar(valor_face * (fator - 1))
    iof = 0.0
    if iof_ativo:
        iof = arredondar(valor_face * (float(iof_base) / 100) + valor_face * (float(iof_diario) / 100) * dias)
    return juros, iof, arredondar(valor_face - juros - iof)


def calcular_comissao(juros_total, comissao, taxa_mensal):
    """Igual ao calcularComissao() da tela: `comissao` pontos dos `taxa_mensal` pontos da
    taxa (2 de 8% = 25% dos juros)."""
    if not comissao or comissao <= 0 or not taxa_mensal or taxa_mensal <= 0:
        return 0.0
    return arredondar(arredondar(juros_total) * comissao / taxa_mensal)


def linha_comissao(data, origem, valor, rotulo, parte, **vinculo):
    """Comissao: sai do caixa no mesmo dia (ao contrario dos juros, que so entram quando o
    cheque e' pago)."""
    return Transaction(date=data, amount=-valor, type='saida', origin=origem, category='Comissão',
                       description=f"Comissão ({parte} dos juros) - {rotulo}"[:200], **vinculo)


def linha_informativa(data, origem, valor, tipo, descricao, **vinculo):
    """Linha so para leitura: amount 0, nao mexe em saldo nem em soma nenhuma."""
    return Transaction(date=data, amount=0.0, valor_informativo=valor, type=tipo, origin=origem,
                       category='Informativo', description=descricao[:200], **vinculo)


def agrupar(linhas):
    """Grava as linhas e liga todas pelo id da primeira (o caixa mostra o bloco junto)."""
    db.session.add_all(linhas)
    db.session.flush()
    grupo = min(t.id for t in linhas)
    for t in linhas:
        t.grupo_id = grupo


def ler_comissao(data, taxa_mensal):
    try:
        comissao = float(data.get('comissao') or 0)
    except (TypeError, ValueError):
        raise ValueError("Comissão inválida")
    if not 0 <= comissao <= taxa_mensal:
        raise ValueError(f"A comissão tem que ficar entre 0 e a taxa do borderô ({taxa_mensal:g}%)")
    return comissao


class OperationService:
    def __init__(self):
        self.audit = AuditService()

    def create_operation(self, data):
        try:
            client = Client.query.get(data['client_id'])
            if not client:
                raise ValueError("Cliente não encontrado")
            if not data.get('checks'):
                raise ValueError("Borderô sem cheques")

            op_date = data.get('operation_date', datetime.now().date())
            if isinstance(op_date, str):
                op_date = datetime.strptime(op_date, '%Y-%m-%d').date()

            conta_origem = data.get('account_source', 'Dinheiro') 
            origem_sistema = f"Sistema ({conta_origem})"
            
            dias_comp = int(data.get('dias_compensacao', 0))
            comissao = ler_comissao(data, float(data['taxa_mensal']))

            new_operation = Operation(
                client_id=data['client_id'],
                client_name_snapshot=client.name,
                operation_date=op_date,
                monthly_rate=float(data['taxa_mensal']), 
                compensation_days=dias_comp,
                account_source=conta_origem,
                notes=data.get('notes'),
                comissao=comissao,
                total_face_value=0.0,
                total_interest=0.0,
                total_net_value=0.0
            )
            
            db.session.add(new_operation)
            db.session.flush()

            acumulado_face = 0.0
            acumulado_juros = 0.0
            acumulado_iof = 0.0
            acumulado_liquido = 0.0
            
            checks_audit_list = []

            # IOF como a tela usou; tela antiga (sem esses campos) cai nas configuracoes
            config = CompanySettings.query.first()
            iof_ativo = bool(data.get('iof_enabled', float(data.get('iof_amount') or 0) > 0))
            iof_base = float(data.get('iof_base', config.iof_rate if config else 0.38) or 0)
            iof_diario = float(data.get('iof_diario', config.iof_daily_rate if config else 0.0041) or 0)

            for n, item in enumerate(data['checks'], 1):
                valor_face = float(item['valor'])
                if not valor_face > 0:
                    raise ValueError(f"Cheque {n}: o valor tem que ser maior que zero")
                vencimento = item['vencimento']
                venc = datetime.strptime(vencimento, '%Y-%m-%d').date() if isinstance(vencimento, str) else vencimento
                dias = (venc - new_operation.operation_date).days + dias_comp

                juros, iof, liquido = calcular_linha(valor_face, dias, new_operation.monthly_rate,
                                                     iof_ativo, iof_base, iof_diario)
                # A tela manda o que mostrou. Tem que bater com a conta daqui (1 centavo de
                # folga so para o arredondamento de ponto flutuante JS x Python) - e ai grava
                # EXATAMENTE o que a tela mostrou, centavo por centavo.
                if item.get('juros') is not None and item.get('liquido') is not None:
                    j_tela = round(float(item['juros']), 2)
                    i_tela = round(float(item.get('iof') or 0), 2)
                    l_tela = round(float(item['liquido']), 2)
                    if (abs(j_tela - juros) > 0.011 or abs(i_tela - iof) > 0.011
                            or abs(round(valor_face - j_tela - i_tela, 2) - l_tela) > 0.011):
                        raise ValueError(f"Cheque {n}: a conta da tela (juros {j_tela:.2f}, IOF {i_tela:.2f}) "
                                         f"não confere com o servidor (juros {juros:.2f}, IOF {iof:.2f}). "
                                         f"Recarregue a página e gere o borderô de novo.")
                    juros, iof, liquido = j_tela, i_tela, l_tela
                if not liquido > 0:
                    raise ValueError(f"Cheque {n}: juros e IOF passam do valor do cheque (líquido R$ {liquido:.2f}). "
                                     f"Use menos parcelas ou um prazo menor.")

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
                acumulado_iof += iof
                acumulado_liquido += liquido
                
                checks_audit_list.append(f"[{new_check.bank} R$ {new_check.amount:.2f}]")

            new_operation.total_face_value = round(acumulado_face, 2)
            new_operation.total_interest = round(acumulado_juros, 2)
            new_operation.comissao_valor = calcular_comissao(new_operation.total_interest, comissao,
                                                             new_operation.monthly_rate)
            # o que sai do caixa = soma dos liquidos dos cheques = o liquido da tela
            # (valor - juros - IOF). Tudo sai dos mesmos numeros: nao sobra centavo.
            liquido_entregue = round(acumulado_liquido, 2)
            new_operation.iof_amount = round(acumulado_iof, 2)
            new_operation.total_net_value = liquido_entregue

            transaction = Transaction(
                date=new_operation.operation_date,
                description=f"Pgto Borderô #{new_operation.id} - {client.name}",
                amount=liquido_entregue * -1,
                type='saida',
                origin=origem_sistema,
                category='Compra de Ativos',
                operation_id=new_operation.id
            )
            if new_operation.comissao_valor > 0:
                # o cliente recebe o liquido e a comissao e' paga a parte: a 1a linha (so
                # informativa) e' o total que sai do banco = cliente + comissao, as de baixo
                rotulo = f"Borderô #{new_operation.id} - {client.name}"
                transaction.description = f"Cliente recebe - {rotulo}"[:200]
                agrupar([
                    linha_informativa(new_operation.operation_date, origem_sistema,
                                      round(liquido_entregue + new_operation.comissao_valor, 2), 'saida',
                                      f"Pgto {rotulo}", operation_id=new_operation.id),
                    transaction,
                    linha_comissao(new_operation.operation_date, origem_sistema, new_operation.comissao_valor,
                                   rotulo, f"{comissao / new_operation.monthly_rate * 100:.4g}%".replace('.', ','),
                                   operation_id=new_operation.id),
                ])
            else:
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
                + (f"Comissão: {comissao:g} de {new_operation.monthly_rate:g} pontos da taxa "
                   f"({comissao / new_operation.monthly_rate * 100:.1f}% dos juros) = "
                   f"R$ {new_operation.comissao_valor:.2f}\n" if comissao else '') +
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

    def get_paginated(self, page=1, per_page=20, sort_by='id', sort_order='desc'):
        """
        Lista de borderôs paginada.

        Antes existia só o get_all(), que devolvia TODOS os borderôs com TODOS os
        cheques de uma vez. Com os 6 borderôs de teste isso era instantâneo; depois
        de importar a planilha (4.113 borderôs / 8.665 cheques) virou uma resposta
        de ~7 MB e ~7 s a cada abertura da tela de Borderô.
        A tela (BorderoView.fetchNextId) já lia 'items' e 'total' — só que ninguém
        os enviava. Agora envia. O get_all() acima continua existindo intacto.
        """
        colunas = {
            'id': Operation.id,
            'operation_date': Operation.operation_date,
            'total_face_value': Operation.total_face_value,
        }
        coluna = colunas.get(sort_by, Operation.id)
        ordem = desc(coluna) if str(sort_order).lower() == 'desc' else asc(coluna)

        paginacao = Operation.query\
            .options(joinedload(Operation.checks))\
            .order_by(ordem)\
            .paginate(page=page, per_page=per_page, error_out=False)

        return {
            'items': paginacao.items,
            'total': paginacao.total,
            'pages': paginacao.pages,
            'current_page': page,
        }

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
        