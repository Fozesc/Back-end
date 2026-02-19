from flask_marshmallow import Marshmallow
from marshmallow import fields
from app.models.domain import CompanySettings

ma = Marshmallow()

class SettingsSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = CompanySettings
        load_instance = True
    
    
    capital_social = fields.Float()
    saldo_inicial_bb = fields.Float()
    saldo_inicial_ce = fields.Float()
    saldo_inicial_caixa = fields.Float()

settings_schema = SettingsSchema()