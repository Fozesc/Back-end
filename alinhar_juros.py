"""
Alinha o juros gravado nos cheques com a conta da tela do Borderô.

Ate 29/09/2026 o servidor gravava em cada cheque o juros de outra formula
(liquido = valor / fator) em vez da conta da tela (juros = valor x (fator - 1)).
O caixa estava certo (lancava o liquido da tela); o juros por cheque - que alimenta
lucro, relatorios e Historico Mensal - ficava menor.

Para cada borderô criado no sistema (os importados da planilha ficam como estao) refaz
a conta da tela com os dados gravados (valor de face, dias, taxa, IOF). So corrige se a
soma dos liquidos refeitos bater, centavo por centavo, com o que saiu do caixa naquele
dia - e' a prova de que os numeros sao os mesmos que a tela mostrou. Se nao bater, nao
mexe e lista o borderô para conferir a mao.

  python alinhar_juros.py            simula (nao grava nada)
  python alinhar_juros.py --gravar   grava
"""
import sys

from app import create_app, db
from app.models.domain import AuditLog, Check, CompanySettings, Operation
from app.services.operation_service import calcular_linha

TAG_IMPORT = 'IMPORT-PLANILHA'


def main(gravar, app=None):
    app = app or create_app()
    with app.app_context():
        config = CompanySettings.query.first()
        iof_base = config.iof_rate if config else 0.38
        iof_diario = config.iof_daily_rate if config else 0.0041

        ops = (Operation.query.filter(db.or_(Operation.notes.is_(None),
                                             ~Operation.notes.like(f'%{TAG_IMPORT}%')))
               .order_by(Operation.id).all())
        corrigir, certos, nao_bate = [], 0, []
        for op in ops:
            cheques = Check.query.filter_by(operation_id=op.id).order_by(Check.id).all()
            if not cheques:
                continue
            iof_ativo = (op.iof_amount or 0) > 0
            novos = []
            for c in cheques:
                face = c.original_amount if c.original_amount is not None else c.amount
                juros, iof, liquido = calcular_linha(face, c.days or 0, op.monthly_rate or 0,
                                                     iof_ativo, iof_base, iof_diario)
                novos.append((c, juros, iof, liquido))
            soma_liquido = round(sum(n[3] for n in novos), 2)
            soma_iof = round(sum(n[2] for n in novos), 2)
            if abs(soma_liquido - round(op.total_net_value or 0, 2)) > 0.005 or \
                    abs(soma_iof - round(op.iof_amount or 0, 2)) > 0.005:
                nao_bate.append((op, soma_liquido, soma_iof))
                continue
            if all(round(c.interest_amount or 0, 2) == j and round(c.net_amount or 0, 2) == l
                   for c, j, _, l in novos) and round(op.total_interest or 0, 2) == round(sum(n[1] for n in novos), 2):
                certos += 1
                continue
            corrigir.append((op, novos))

        print(f"Borderôs criados no sistema com cheque: {certos + len(corrigir) + len(nao_bate)}")
        print(f"  ja estavam certos ..........: {certos}")
        print(f"  a corrigir (conferem c/ caixa): {len(corrigir)}")
        print(f"  NAO batem com o caixa .......: {len(nao_bate)}  (nao mexo)")
        for op, novos in corrigir:
            antes = round(sum(c.interest_amount or 0 for c, *_ in novos), 2)
            depois = round(sum(n[1] for n in novos), 2)
            print(f"\n  Borderô #{op.id} {op.operation_date} {op.client_name_snapshot} - {len(novos)} cheque(s), "
                  f"{op.monthly_rate}% a.m. | juros {antes:.2f} -> {depois:.2f} ({depois - antes:+.2f})")
            for c, j, i, l in novos:
                print(f"     #{c.id} {c.number or 'S/N':8s} face {c.original_amount or c.amount:>11,.2f} {c.days:>4}d | "
                      f"juros {c.interest_amount or 0:>9,.2f} -> {j:>9,.2f} | liquido {c.net_amount or 0:>10,.2f} -> {l:>10,.2f}")
        for op, soma_liquido, soma_iof in nao_bate:
            print(f"\n  [!] Borderô #{op.id} {op.operation_date} {op.client_name_snapshot}: saiu do caixa "
                  f"{op.total_net_value or 0:,.2f}, a conta da tela da {soma_liquido:,.2f} "
                  f"(IOF gravado {op.iof_amount or 0:,.2f} x {soma_iof:,.2f}) - confira a mao")

        if not gravar:
            print("\nSIMULACAO: nada foi gravado. Para gravar: python alinhar_juros.py --gravar")
            return
        for op, novos in corrigir:
            antes = round(sum(c.interest_amount or 0 for c, *_ in novos), 2)
            for c, j, _, l in novos:
                c.interest_amount, c.net_amount = j, l
            op.total_interest = round(sum(n[1] for n in novos), 2)
            db.session.add(AuditLog(user_name='Sistema', action='UPDATE', target='Borderô',
                                    description=f"Borderô #{op.id}: juros dos cheques alinhado com a conta da "
                                                f"tela ({antes:.2f} -> {op.total_interest:.2f}). Caixa e valores "
                                                f"dos cheques nao mudaram."))
        db.session.commit()
        print(f"\nGRAVADO: {len(corrigir)} borderô(s) corrigido(s).")


if __name__ == '__main__':
    main('--gravar' in sys.argv)
