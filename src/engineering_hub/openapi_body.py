"""Describe manually parsed JSON bodies without changing request handling."""


def request_body(model):
    schema = model.model_json_schema()
    definitions = schema.pop('$defs', {})

    def inline(value, seen=frozenset()):
        if isinstance(value, list):
            return [inline(item, seen) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            ref = value['$ref']
            prefix = '#/$defs/'
            if not ref.startswith(prefix) or ref[len(prefix):] not in definitions:
                raise ValueError('Unsupported request schema reference')
            name = ref[len(prefix):]
            if name in seen:
                raise ValueError('Recursive request schema is unsupported')
            resolved = inline(definitions[name], seen | {name})
            return {**resolved, **{k: inline(v, seen) for k, v in value.items() if k != '$ref'}}
        return {key: inline(item, seen) for key, item in value.items()}

    return {'requestBody': {'required': True, 'content': {'application/json': {'schema': inline(schema)}}}}
