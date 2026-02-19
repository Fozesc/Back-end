from flask_marshmallow import Marshmallow
from marshmallow import fields
from app.models.domain import Operation
from app.schemas.check_schema import CheckSchema

ma = Marshmallow()

class OperationSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Operation
        load_instance = True
        include_fk = True
    

    total_face_value = fields.Float()
    total_interest = fields.Float()
    total_net_value = fields.Float()
    monthly_rate = fields.Float()
    
   
    total_net = fields.Float(attribute='total_net_value', dump_only=True)
  
    
    checks = fields.Nested(CheckSchema, many=True)
    operation_date = fields.Date(format='%Y-%m-%d')

operation_schema = OperationSchema()
operations_schema = OperationSchema(many=True)