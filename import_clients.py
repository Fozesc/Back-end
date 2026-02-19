import pandas as pd
import os
import re
from app import create_app, db
from app.models.domain import Client

# --- CONFIGURAÇÕES ---
ARQUIVO_EXCEL = "Borderô Fozesc.xlsx"
NOME_DA_ABA = "CLIENTES" 

def normalizar_nome(nome):
    if not nome: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(nome)).lower()

def limpar_cpf(valor):
    """
    Limpa o CPF e garante que não estoure o limite do banco.
    Se for uma frase longa (lixo), retorna None.
    """
    if pd.isna(valor): return None
    s = str(valor).strip()
    
    # Se for lixo ou rodapé (ex: '-', 'nan', '0')
    if s.lower() in ['-', 'nan', '0', '0.0', 'nt', 's/n']: return None
    
    # PROTEÇÃO: Se for muito longo (frase), ignora
    if len(s) > 18: 
        return None # Ignora frases no lugar de CPF
        
    return s

def limpar_valor(valor):
    if pd.isna(valor): return 0.0
    s = str(valor).replace('R$', '').replace('.', '').replace(',', '.').strip()
    try: return float(s)
    except: return 0.0

def limpar_taxa(valor):
    if pd.isna(valor): return 4.0 
    s = str(valor).replace('%', '').replace(',', '.').strip()
    try: return float(s)
    except: return 4.0 

def importar():
    if not os.path.exists(ARQUIVO_EXCEL):
        print(f"❌ Arquivo '{ARQUIVO_EXCEL}' não encontrado.")
        return

    app = create_app()
    with app.app_context():
        try:
            print("🔍 Lendo planilha...")
            df = pd.read_excel(ARQUIVO_EXCEL, sheet_name=NOME_DA_ABA, dtype={'CPF / CNPJ': str})
            df.columns = df.columns.str.strip()
        except Exception as e:
            print(f"❌ Erro ao ler Excel: {e}")
            return

        print(f"🚀 Iniciando Importação Segura...")
        
        # Carrega clientes existentes para comparação
        clientes_banco = Client.query.all()
        print(f"📦 Banco atual: {len(clientes_banco)} clientes.")

        atualizados = 0
        criados = 0
        ignorados = 0
        
        for index, row in df.iterrows():
            linha_excel = index + 2
            try:
                nome_bruto = str(row.get('CLIENTES', '')).strip()
                
                # --- FILTROS DE LIXO ---
                # Pula se for vazio ou se for linha de rodapé (começa com *)
                if not nome_bruto or nome_bruto == 'nan' or nome_bruto.startswith('*'):
                    ignorados += 1
                    continue

                nome_norm = normalizar_nome(nome_bruto)
                
                cpf_novo = limpar_cpf(row.get('CPF / CNPJ'))
                taxa_nova = limpar_taxa(row.get('TAXA'))
                limite_novo = limpar_valor(row.get('LIMITE R$'))

                # --- BUSCA INTELIGENTE (FUZZY MATCH) ---
                cliente_encontrado = None

                for c in clientes_banco:
                    nome_banco_norm = normalizar_nome(c.name)
                    # Verifica se nomes batem
                    if nome_banco_norm == nome_norm or \
                       (len(nome_banco_norm) > 5 and nome_banco_norm in nome_norm) or \
                       (len(nome_norm) > 5 and nome_norm in nome_banco_norm):
                        cliente_encontrado = c
                        break
                
                if cliente_encontrado:
                    # ATUALIZA
                    alterou = False
                    if cpf_novo and cliente_encontrado.document != cpf_novo:
                        cliente_encontrado.document = cpf_novo
                        alterou = True
                    if taxa_nova != 4.0: 
                        cliente_encontrado.standard_rate = taxa_nova
                        alterou = True
                    if limite_novo > 0:
                        cliente_encontrado.credit_limit = limite_novo
                        alterou = True
                    
                    if alterou:
                        atualizados += 1
                else:
                    # CRIA NOVO
                    novo = Client(
                        name=nome_bruto,
                        document=cpf_novo,
                        standard_rate=taxa_nova,
                        credit_limit=limite_novo,
                        notes="Importado via Excel"
                    )
                    db.session.add(novo)
                    clientes_banco.append(novo) # Adiciona na lista local para não duplicar
                    criados += 1

            except Exception as e:
                print(f"⚠️ Erro leve na linha {linha_excel}: {e}")

        try:
            db.session.commit()
            print("="*40)
            print(f"✅ FINALIZADO COM SUCESSO!")
            print(f"🔄 Clientes Atualizados: {atualizados}")
            print(f"➕ Clientes Novos: {criados}")
            print(f"🗑️ Linhas Ignoradas (Lixo/Rodapé): {ignorados}")
            print("="*40)
        except Exception as e:
            print(f"❌ Erro fatal ao salvar: {e}")

if __name__ == "__main__":
    importar()