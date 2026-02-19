from flask_marshmallow import Marshmallow
from marshmallow import fields
from app.models.domain import Check

ma = Marshmallow()

class CheckSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Check
        load_instance = True
        include_fk = True

   
    due_date = fields.Date(format='%Y-%m-%d')
    
   
    amount = fields.Float()
    interest_amount = fields.Float()
    net_amount = fields.Float()

check_schema = CheckSchema()
checks_schema = CheckSchema(many=True)