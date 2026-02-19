from app.models.domain import CompanySettings
from app import db

class SettingsService:
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
       
        if 'nomeEmpresa' in data: settings.company_name = data['nomeEmpresa']
        if 'cnpj' in data: settings.cnpj = data['cnpj']
        if 'telefone' in data: settings.phone = data['telefone']
        if 'endereco' in data: settings.address = data['endereco']
        
        if 'taxaPadrao' in data: settings.default_monthly_rate = float(data['taxaPadrao'])
        if 'diasCompensacaoPadrao' in data: settings.default_compensation_days = int(data['diasCompensacaoPadrao'])
        
    
        if 'iof_rate' in data: settings.iof_rate = float(data['iof_rate'])
        if 'extension_rate' in data: settings.extension_rate = float(data['extension_rate'])
        if 'fine_rate' in data: settings.fine_rate = float(data['fine_rate'])

        db.session.commit()
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