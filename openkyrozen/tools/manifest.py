"""Authoritative tool contracts bound to one workspace/runtime."""
from __future__ import annotations

import json
import re
from collections.abc import MutableMapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Callable

from openkyrozen.security.capabilities import CapabilityToken


def _json_value(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('Schema object keys must be strings')
        for item in value.values():
            _json_value(item)
    elif isinstance(value, list):
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in {str, bool, int, float}:
        raise ValueError('Schema values must be JSON values')


def _schema_shape(schema):
    if not isinstance(schema, dict):
        raise ValueError('Schema must be an object')
    supported = {'type','properties','required','additionalProperties','items','enum','anyOf','oneOf','allOf',
                 'description','title','default','examples','minLength','maxLength','minItems','maxItems',
                 'minProperties','maxProperties','minimum','maximum','exclusiveMinimum','exclusiveMaximum',
                 'multipleOf','pattern','format','uniqueItems'}
    if set(schema) - supported:
        raise ValueError('Unsupported schema keyword')
    for key in ('description','title','pattern','format'):
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
        _schema_shape(item)
    if 'items' in schema:
        _schema_shape(schema['items'])
    for keyword in ('anyOf','oneOf','allOf'):
        if keyword in schema:
            if not isinstance(schema[keyword],list) or not schema[keyword]:
                raise ValueError(f'{keyword} must be a nonempty schema array')
            for item in schema[keyword]:
                _schema_shape(item)



@dataclass(frozen=True)
class ToolManifest:
    name: str
    capability: str
    risk: str = "normal"
    version: str = "1.0.0"
    source: str = "builtin"


@dataclass(frozen=True, init=False)
class ToolSpec:
    name: str
    description: str
    capability: str
    side_effects: str
    parallel_safe: bool
    executor: Callable[..., Any]
    visibility: str
    aliases: tuple[str, ...]
    risk: str
    version: str
    source: str
    legacy_fields: tuple[str, ...]
    legacy_argument_aliases: tuple[tuple[str, str], ...]
    confirmation_surfaces: tuple[str, ...]
    _schema_json: str

    def __init__(self, name, executor, *, description, input_schema, capability,
                 side_effects, parallel_safe=False, visibility="public", aliases=(),
                 risk="normal", version="1.0.0", source="builtin", legacy_fields=(),
                 legacy_argument_aliases=(), confirmation_surfaces=('cli','tui')):
        for label, value in (("name",name),("description",description),("version",version),("source",source)):
            if not isinstance(value,str) or not value.strip():
                raise ValueError(f"ToolSpec {label} must be a nonempty string")
        if not name.isidentifier() or not callable(executor):
            raise ValueError("ToolSpec requires an identifier and callable executor")
        if capability not in {"read","write","shell","network","git","browser","destructive","dynamic"}:
            raise ValueError("Unknown tool capability")
        if side_effects not in {"none","read","mutation","external","stateful","internal_write","orchestration","unknown"}:
            raise ValueError("Unknown tool side-effect class")
        if type(parallel_safe) is not bool or visibility not in {"public","hidden"} or risk not in {"normal","high"}:
            raise ValueError("Invalid tool policy metadata")
        if parallel_safe and side_effects not in {"none","read"}:
            raise ValueError("Stateful tools cannot declare parallel safety")
        try:
            _json_value(input_schema)
            _schema_shape(input_schema)
            schema_json=json.dumps(input_schema,allow_nan=False,sort_keys=True)
            schema=json.loads(schema_json)
        except (TypeError,ValueError,RecursionError) as exc:
            raise ValueError("Tool schema must be JSON-safe") from exc
        if not isinstance(schema,dict) or schema.get('type')!='object' or not isinstance(schema.get('properties'),dict):
            raise ValueError("Tool schema must describe an object with properties")
        required=schema.get('required',[])
        if not isinstance(required,list) or any(not isinstance(k,str) or k not in schema['properties'] for k in required) or len(set(required))!=len(required):
            raise ValueError("Invalid required tool properties")
        if schema.get('additionalProperties') is not False:
            raise ValueError("Tool schema must reject additional properties")
        for key,value in schema['properties'].items():
            if not isinstance(value,dict) or (value.get('type') not in {'string','integer','number','boolean','object','array','null'} and not any(key in value for key in ('anyOf','oneOf','allOf'))):
                raise ValueError("Invalid tool property schema")
        if isinstance(aliases,str) or isinstance(legacy_fields,str) or isinstance(legacy_argument_aliases,str):
            raise ValueError('Alias and conversion metadata must be collections')
        alias_values=tuple(aliases)
        if any(not isinstance(a,str) or not a.isidentifier() or a.casefold()==name.casefold() for a in alias_values) or len({a.casefold() for a in alias_values})!=len(alias_values):
            raise ValueError("Invalid tool aliases")
        legacy_values=tuple(legacy_fields)
        argument_aliases=tuple(tuple(pair) for pair in legacy_argument_aliases)
        if any(field not in schema['properties'] or schema['properties'][field].get('type') != 'string' for field in legacy_values):
            raise ValueError("Legacy conversion fields must match schema")
        if len(set(argument_aliases)) != len(argument_aliases) or any(len(pair)!=2 or pair[0] not in legacy_values or not isinstance(pair[1],str) or pair[1] in legacy_values for pair in argument_aliases):
            raise ValueError("Invalid legacy argument aliases")
        confirmation_surfaces=tuple(confirmation_surfaces)
        if any(s not in {'cli','tui'} for s in confirmation_surfaces):
            raise ValueError('Invalid confirmation surface')
        values=locals()
        for field in fields(self):
            value = schema_json if field.name=='_schema_json' else alias_values if field.name=='aliases' else legacy_values if field.name=='legacy_fields' else argument_aliases if field.name=='legacy_argument_aliases' else values[field.name]
            object.__setattr__(self,field.name,value)

    @property
    def input_schema(self):
        return json.loads(self._schema_json)

    @property
    def manifest(self):
        return ToolManifest(self.name,self.capability,self.risk,self.version,self.source)

    def replace(self, **changes):
        values={field.name:getattr(self,field.name) for field in fields(self) if field.name!='_schema_json'}
        return ToolSpec(**(values|{'input_schema':self.input_schema}|changes))

    def legacy_arguments(self, arguments):
        if arguments is None:
            return ''
        if isinstance(arguments,str):
            return arguments
        if not isinstance(arguments,dict):
            raise ValueError('arguments must be an object or plain string')
        if not arguments:
            return ''
        if set(arguments)=={'args'}:
            if not isinstance(arguments['args'],str):
                raise ValueError('arguments.args must be a string')
            return arguments['args']
        if not self.legacy_fields:
            raise ValueError(f"tool '{self.name}' accepts object arguments only as {{'args': '<string>'}}")
        schema=self.input_schema
        values=[]
        for field in self.legacy_fields:
            names=(field,)+tuple(alias for canonical,alias in self.legacy_argument_aliases if canonical==field)
            present=next((name for name in names if name in arguments),None)
            if present is None:
                if field in schema.get('required',[]):
                    raise ValueError(f'missing required argument: {field}')
                values.append('')
            else:
                if not isinstance(arguments[present],str):
                    raise ValueError(f"argument '{present}' must be a string")
                values.append(arguments[present])
        if self.name in {'find_files','git_clone'} and not values[-1]:
            values.pop()
        return '|'.join(values)


class ToolMapping(MutableMapping):
    def __init__(self, registry, excluded=()):
        self.registry=registry
        self.excluded=frozenset(excluded)
    def __getitem__(self,name):
        if name in self.excluded:
            raise KeyError(name)
        return self.registry.get_spec(name).executor
    def __setitem__(self,name,function):
        if name in self.excluded:
            raise PermissionError('Tool is excluded from this view')
        self.registry.register(name,function)
    def __delitem__(self,name):
        if name in self.excluded:
            raise KeyError(name)
        self.registry.unregister(name)
    def __iter__(self):
        return iter(tuple(name for name in self.registry._specs if name not in self.excluded))
    def __len__(self):
        return sum(1 for _ in self)


class ToolRegistry:
    def __init__(self, tools=None):
        self._specs={}
        self.runtime_bindings_initialized=False
        self.tools=ToolMapping(self)
        for name,executor in (tools or {}).items():
            self.register(name,executor)

    @property
    def specs(self):
        return MappingProxyType(self._specs)

    @property
    def manifests(self):
        return MappingProxyType({name:spec.manifest for name,spec in self._specs.items()})

    @property
    def aliases(self):
        return {alias:name for name,spec in self._specs.items() for alias in spec.aliases}

    def resolve_legacy_name(self,name):
        identities={key.casefold():key for key in self._specs}
        identities.update({alias.casefold():canonical for alias,canonical in self.aliases.items()})
        return identities.get(name.casefold(),name)

    def get_spec(self,name):
        return self._specs[name]

    def register_spec(self,spec,*,replace=False):
        if not isinstance(spec,ToolSpec):
            raise TypeError('Registration requires ToolSpec')
        if spec.name in self._specs and not replace:
            raise ValueError(f'Tool already registered: {spec.name}')
        occupied={value.casefold() for value in (set(self._specs)|set(self.aliases))}
        previous=self._specs.get(spec.name)
        if previous:
            occupied-={value.casefold() for value in (*previous.aliases,previous.name)}
        if spec.name.casefold() in occupied or any(alias.casefold() in occupied for alias in spec.aliases):
            raise ValueError('Tool name or alias collision')
        self._specs[spec.name]=spec

    def register(self,name,function,*,capability=None,risk=None,version=None,source=None):
        from .catalog import builtin_metadata
        old=self._specs.get(name)
        if old:
            updates={'executor':function}
            updates.update({k:v for k,v in {'capability':capability,'risk':risk,'version':version,'source':source}.items() if v is not None})
            spec=old.replace(**updates)
        else:
            metadata=builtin_metadata(name) or {'capability':'dynamic','side_effects':'unknown','parallel_safe':False,
                'input_schema':{'type':'object','properties':{'args':{'type':'string'}},'additionalProperties':False},'source':'legacy'}
            metadata.update({k:v for k,v in {'capability':capability,'risk':risk,'version':version,'source':source}.items() if v is not None})
            spec=ToolSpec(name,function,description=(function.__doc__ or '').strip() or f'Invoke {name}.',**metadata)
        self.register_spec(spec,replace=old is not None)

    def unregister(self,name):
        del self._specs[name]

    def snapshot(self,names=None):
        return {name:spec for name,spec in self._specs.items() if names is None or name in names}

    def catalog(self,token=None,*,names=None):
        return [dict(name=s.name,capability=s.capability,risk=s.risk,version=s.version,source=s.source,
                     description=s.description,side_effects=s.side_effects,parallel_safe=s.parallel_safe,visibility=s.visibility)
                for s in self._visible(token,names)]

    def _visible(self,token,names):
        return (s for name,s in sorted(self._specs.items()) if s.visibility=='public' and (names is None or name in names)
                and (token is None or token.allows(s.capability)))

    def mcp_descriptors(self,token=None,*,names=None):
        return [{'name':s.name,'description':s.description.splitlines()[0],'inputSchema':s.input_schema} for s in self._visible(token,names)]

    def provider_schemas(self,token=None,*,names=None):
        return [{'name':s.name,'description':s.description.splitlines()[0],'parameters':s.input_schema} for s in self._visible(token,names)]

    def invoke(self,name,args,token):
        spec=self.get_spec(name)
        if not token.allows(spec.capability):
            raise PermissionError(f"Capability '{spec.capability}' is not granted by token")
        return spec.executor(args)
