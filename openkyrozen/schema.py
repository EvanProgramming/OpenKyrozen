"""Pure schema-shape checks shared by tool contracts and provider adapters."""
import re


def validate_schema(schema, *, openapi=False):
    if not isinstance(schema, dict):
        raise ValueError('Schema must be an object')
    supported = {'type','properties','required','additionalProperties','items','enum','anyOf','oneOf','allOf',
                 'description','title','default','examples','minLength','maxLength','minItems','maxItems',
                 'minProperties','maxProperties','minimum','maximum','exclusiveMinimum','exclusiveMaximum',
                 'multipleOf','pattern','uniqueItems'}
    if openapi:
        supported |= {'format', 'nullable'}
        if 'format' in schema and not isinstance(schema['format'], str):
            raise ValueError('format must be a string')
        if 'nullable' in schema and type(schema['nullable']) is not bool:
            raise ValueError('nullable must be a boolean')
    if set(schema) - supported:
        raise ValueError('Unsupported schema keyword')
    for key in ('description','title','pattern'):
        if key in schema and not isinstance(schema[key],str):
            raise ValueError(f'{key} must be a string')
    if 'pattern' in schema:
        try:
            re.compile(schema['pattern'])
        except re.error as exc:
            raise ValueError('Invalid schema pattern') from exc
    for key in ('minLength','maxLength','minItems','maxItems','minProperties','maxProperties'):
        if key in schema and (type(schema[key]) is not int or schema[key]<0):
            raise ValueError(f'{key} must be a nonnegative integer')
    for key in ('minimum','maximum','exclusiveMinimum','exclusiveMaximum','multipleOf'):
        if key in schema and (type(schema[key]) not in {int,float} or (key=='multipleOf' and schema[key]<=0)):
            raise ValueError(f'Invalid {key} constraint')
    if 'uniqueItems' in schema and type(schema['uniqueItems']) is not bool:
        raise ValueError('uniqueItems must be a boolean')
    if 'additionalProperties' in schema and type(schema['additionalProperties']) is not bool:
        raise ValueError('additionalProperties must be a boolean')
    kinds = {'string','integer','number','boolean','object','array','null'}
    kind = schema.get('type')
    if openapi and isinstance(kind, str):
        kind = kind.lower()
    if 'type' in schema and (not isinstance(kind,str) or kind not in kinds):
        raise ValueError('Invalid schema type')
    if 'examples' in schema and not isinstance(schema['examples'],list):
        raise ValueError('Schema examples must be an array')
    if 'enum' in schema and (not isinstance(schema['enum'],list) or not schema['enum']):
        raise ValueError('Schema enum must be a nonempty array')
    properties = schema.get('properties',{})
    if not isinstance(properties,dict):
        raise ValueError('Schema properties must be an object')
    required = schema.get('required',[])
    if not isinstance(required,list) or any(not isinstance(k,str) or k not in properties for k in required) or len(set(required))!=len(required):
        raise ValueError('Invalid required tool properties')
    for item in properties.values():
        validate_schema(item, openapi=openapi)
    if 'items' in schema:
        validate_schema(schema['items'], openapi=openapi)
    for keyword in ('anyOf','oneOf','allOf'):
        if keyword in schema:
            if not isinstance(schema[keyword],list) or not schema[keyword]:
                raise ValueError(f'{keyword} must be a nonempty schema array')
            for item in schema[keyword]:
                validate_schema(item, openapi=openapi)

