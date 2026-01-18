from app import ma
from app.models.domain import Operation, Check
from marshmallow import fields

class CheckSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Check
        load_instance = True
        include_fk = True

class OperationSchema(ma.SQLAlchemyAutoSchema):
    checks = ma.Nested(CheckSchema, many=True)
    
    class Meta:
        model = Operation
        load_instance = True


class CreateOperationSchema(ma.Schema):
    client_id = fields.Int(allow_none=True)
    client_name_snapshot = fields.Str(required=True)
    operation_date = fields.Date(required=True)
    taxa_mensal = fields.Float(required=True)
    dias_compensacao = fields.Int(required=True)
    
   
    checks = fields.List(fields.Dict(keys=fields.Str(), values=fields.Raw()))

create_operation_schema = CreateOperationSchema()
operation_schema = OperationSchema()