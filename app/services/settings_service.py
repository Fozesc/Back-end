from app.models.domain import CompanySettings
from app import db
from app.services.audit_service import AuditService
from flask_jwt_extended import get_jwt
class SettingsService:

    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
       
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

    def get_settings(self):
      
        settings = CompanySettings.query.first()
        if not settings:
            settings = CompanySettings(
                company_name="Minha Fatoring",
                default_monthly_rate=4.0,
                default_compensation_days=2
            )
            db.session.add(settings)
            db.session.commit()
        
        return self._serialize(settings)

    def update_settings(self, data):
        settings = CompanySettings.query.first()
        if not settings:
            settings = CompanySettings()
            db.session.add(settings)
       
       
        mudancas = []

   
        campos = {
            'nomeEmpresa': ('Nome', 'company_name'),
            'cnpj': ('CNPJ', 'cnpj'),
            'telefone': ('Telefone', 'phone'),
            'endereco': ('Endereço', 'address'),
            'taxaPadrao': ('Taxa Mensal', 'default_monthly_rate'),
            'diasCompensacaoPadrao': ('Dias Comp.', 'default_compensation_days'),
            'iof_rate': ('IOF', 'iof_rate'),
            'extension_rate': ('Juros Prorrogação', 'extension_rate'),
            'fine_rate': ('Multa Devolução', 'fine_rate')
        }

        for chave_json, (label, atributo) in campos.items():
            if chave_json in data:
                valor_novo = data[chave_json]
                valor_antigo = getattr(settings, atributo)

             
                if isinstance(valor_antigo, float): valor_novo = float(valor_novo)
                if isinstance(valor_antigo, int): valor_novo = int(valor_novo)

                if valor_novo != valor_antigo:
                    mudancas.append(f"{label}: {valor_antigo} -> {valor_novo}")
                    setattr(settings, atributo, valor_novo)

        db.session.commit()

   
        if mudancas:
            resumo = "Alterou: " + " | ".join(mudancas)
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Configuracao', resumo)
        
        return self._serialize(settings)

    def _serialize(self, s):
        return {
            'nomeEmpresa': s.company_name,
            'cnpj': s.cnpj,
            'telefone': s.phone,
            'endereco': s.address,
            'taxaPadrao': s.default_monthly_rate,
            'diasCompensacaoPadrao': s.default_compensation_days,
            'iof_rate': s.iof_rate,
            'extension_rate': s.extension_rate,
            'fine_rate': s.fine_rate
        }