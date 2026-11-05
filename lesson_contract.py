"""Validate lesson modules, packets, and help records with the shared schemas.

This module uses only the Python standard library. It reads the same
`contracts/*.schema.json` files as `contracts/validate.mjs` and gives the same
result shape, so the Python server does not need Node or a JSON Schema package.

A result is `{'ok': True}` or `{'ok': False, 'reason': ..., 'errors': [...]}`.
Each error is `{'pointer': ..., 'message': ...}`, where `pointer` is a JSON
Pointer into the document. The reason is one of `invalid`,
`unsupported_version`, `too_large`, or `unreadable`.

A call never changes its input and never writes a file. Files are opened for
reading only.

The schema reader supports the part of JSON Schema draft 2020-12 that the
lesson contracts use. A schema that uses any other keyword raises
`UnsupportedKeywordError` when it is loaded. The validator never skips a
keyword it does not understand, so it cannot pass a document by accident.
"""
import errno
import functools
import json
import os
from pathlib import Path
import re
import stat
import urllib.parse
import warnings

SUPPORTED_SCHEMA_MAJORS = (1,)
LESSON_CONTRACT_KINDS = ('lesson-module', 'packet', 'help-record')
# The largest document, in bytes, that a reader parses. Hosted ingest must use the same cap.
MAX_BYTES = 5 * 1024 * 1024
MAX_ERRORS = 100
HINT_LADDER = ('nudge', 'question', 'partial-example', 'full-answer')
CONTRACTS_DIRECTORY = Path(__file__).resolve().parent / 'contracts'
DRAFT_URI = 'https://json-schema.org/draft/2020-12/schema'


class SchemaError(Exception):
    """A schema is malformed, or cannot be resolved."""


class UnsupportedKeywordError(SchemaError):
    """A schema uses a keyword that this validator does not implement."""


# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

# The characters that the ECMAScript `\s` class matches.
_SPACE_CLASS = '\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'
_LITERAL_ESCAPES = set('\\^$.|?*+()[]{}/-')
_CONTROL_ESCAPES = set('tnrfv')
_HEX = set('0123456789abcdefABCDEF')


def _translate_pattern(pattern):
    """Turn an ECMAScript pattern into a Python pattern with the same meaning.

    Python differs in three ways that matter here: `\\d` and `\\s` follow
    Unicode rules, `$` also matches before a final newline, and `.` skips fewer
    line breaks. Anything this function does not know how to translate raises
    SchemaError, so a pattern is never read with a different meaning.
    """
    out = []
    in_class = False
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == '\\':
            if i + 1 >= n:
                raise SchemaError(f'The pattern {pattern!r} ends with a lone backslash.')
            e = pattern[i + 1]
            if e == 'd':
                out.append('0-9' if in_class else '[0-9]')
            elif e == 's':
                out.append(_SPACE_CLASS if in_class else f'[{_SPACE_CLASS}]')
            elif e == 'u':
                digits = pattern[i + 2:i + 6]
                if len(digits) != 4 or not set(digits) <= _HEX:
                    raise SchemaError(f'The pattern {pattern!r} has a bad \\u escape.')
                out.append(pattern[i:i + 6])
                i += 6
                continue
            elif e in _CONTROL_ESCAPES or e in _LITERAL_ESCAPES:
                out.append('\\' + e)
            else:
                raise SchemaError(f'The pattern {pattern!r} uses the escape \\{e}, which is not supported.')
            i += 2
            continue
        if in_class:
            if c == ']':
                in_class = False
                out.append(c)
            elif c == '[':
                out.append('\\[')
            else:
                out.append(c)
            i += 1
            continue
        if c == '[':
            in_class = True
            out.append(c)
            i += 1
            if pattern.startswith('^', i):
                out.append('^')
                i += 1
            if pattern.startswith(']', i):
                raise SchemaError(f'The pattern {pattern!r} has an empty character class, which is not supported.')
            continue
        if c == '$':
            out.append('\\Z')
        elif c == '.':
            out.append('[^\\n\\r\\u2028\\u2029]')
        elif c == '(':
            if pattern.startswith('(?', i) and not pattern.startswith(('(?:', '(?=', '(?!'), i):
                raise SchemaError(f'The pattern {pattern!r} uses a group form that is not supported.')
            out.append(c)
        elif c == '{' and pattern.startswith('{,', i):
            raise SchemaError(f'The pattern {pattern!r} has a quantifier that is not supported.')
        else:
            out.append(c)
        i += 1
    if in_class:
        raise SchemaError(f'The pattern {pattern!r} has an unclosed character class.')
    return ''.join(out)


def _compile_pattern(pattern):
    translated = _translate_pattern(pattern)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            return re.compile(translated)
    except (re.error, Warning) as error:
        raise SchemaError(f'The pattern {pattern!r} is not valid: {error}') from error


# ---------------------------------------------------------------------------
# Schema registry
# ---------------------------------------------------------------------------

_ANNOTATIONS = frozenset(['$schema', '$id', '$defs', '$comment', 'title', 'description', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly'])
_ASSERTIONS = frozenset([
    '$ref', 'type', 'enum', 'const', 'pattern', 'minLength', 'maxLength', 'minimum', 'maximum',
    'exclusiveMinimum', 'exclusiveMaximum', 'required', 'properties', 'additionalProperties',
    'propertyNames', 'minProperties', 'maxProperties', 'items', 'prefixItems', 'minItems',
    'maxItems', 'uniqueItems', 'oneOf', 'anyOf', 'allOf', 'not', 'if', 'then', 'else',
])
_TYPES = frozenset(['string', 'number', 'integer', 'boolean', 'null', 'object', 'array'])
_SCHEMA_VALUED = ('items', 'additionalProperties', 'propertyNames', 'not', 'if', 'then', 'else')
_SCHEMA_LISTS = ('prefixItems', 'allOf', 'anyOf', 'oneOf')
_SCHEMA_MAPS = ('properties', '$defs')
_NON_NEGATIVE_INTEGERS = ('minLength', 'maxLength', 'minItems', 'maxItems', 'minProperties', 'maxProperties')
_NUMBERS = ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum')


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer(value):
    return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())


def _escape_pointer(segment):
    return str(segment).replace('~', '~0').replace('/', '~1')


def _equal(a, b):
    """JSON equality: 1 equals 1.0, and true does not equal 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    return False


def _canonical(value):
    """A hashable form of a JSON value that follows `_equal`."""
    if isinstance(value, bool):
        return ('b', value)
    if _is_number(value):
        return ('n', value)
    if isinstance(value, str):
        return ('s', value)
    if value is None:
        return ('z',)
    if isinstance(value, list):
        return ('a', tuple(_canonical(item) for item in value))
    if isinstance(value, dict):
        return ('o', frozenset((key, _canonical(item)) for key, item in value.items()))
    raise TypeError(f'Not a JSON value: {type(value).__name__}')


class SchemaRegistry:
    """A set of schemas, found by `$id`, that can validate JSON values.

    Loading checks every schema in full. A keyword that is not implemented, a
    bad value, a pattern that cannot be translated, or a `$ref` that does not
    resolve raises SchemaError before any document is validated.
    """

    def __init__(self, schemas):
        self._schemas = {}
        self._patterns = {}
        for schema in schemas:
            if not isinstance(schema, dict) or not isinstance(schema.get('$id'), str) or not schema['$id']:
                raise SchemaError('Each registered schema must be an object with a string $id.')
            if '#' in schema['$id']:
                raise SchemaError(f"The $id {schema['$id']!r} must not hold a fragment.")
            if schema['$id'] in self._schemas:
                raise SchemaError(f"The $id {schema['$id']!r} is registered twice.")
            self._schemas[schema['$id']] = schema
        for schema_id, schema in self._schemas.items():
            self._check(schema, schema_id, '', is_root=True)

    # -- loading checks --------------------------------------------------

    def _check(self, node, resource, where, is_root=False):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise SchemaError(f'The schema at {where or "/"} in {resource} must be an object or a boolean.')
        for keyword in node:
            if keyword not in _ANNOTATIONS and keyword not in _ASSERTIONS:
                raise UnsupportedKeywordError(
                    f'The keyword {keyword!r} at {where or "/"} in {resource} is not supported by this validator.')
        if '$id' in node and not is_root:
            raise SchemaError(f'A nested $id at {where or "/"} in {resource} is not supported.')
        if '$schema' in node and node['$schema'] != DRAFT_URI:
            raise SchemaError(f'The $schema at {where or "/"} in {resource} must be {DRAFT_URI}.')
        if 'type' in node:
            names = node['type'] if isinstance(node['type'], list) else [node['type']]
            if not names or not all(isinstance(name, str) and name in _TYPES for name in names):
                raise SchemaError(f'The type at {where or "/"} in {resource} is not valid.')
        if 'required' in node and not (isinstance(node['required'], list) and all(isinstance(name, str) for name in node['required'])):
            raise SchemaError(f'The required list at {where or "/"} in {resource} must hold strings.')
        if 'enum' in node and not isinstance(node['enum'], list):
            raise SchemaError(f'The enum at {where or "/"} in {resource} must be a list.')
        if 'uniqueItems' in node and not isinstance(node['uniqueItems'], bool):
            raise SchemaError(f'The uniqueItems at {where or "/"} in {resource} must be a boolean.')
        for keyword in _NON_NEGATIVE_INTEGERS:
            if keyword in node and not (_is_integer(node[keyword]) and node[keyword] >= 0):
                raise SchemaError(f'The {keyword} at {where or "/"} in {resource} must be a non-negative integer.')
        for keyword in _NUMBERS:
            if keyword in node and not _is_number(node[keyword]):
                raise SchemaError(f'The {keyword} at {where or "/"} in {resource} must be a number.')
        if 'pattern' in node:
            if not isinstance(node['pattern'], str):
                raise SchemaError(f'The pattern at {where or "/"} in {resource} must be a string.')
            self._patterns[node['pattern']] = _compile_pattern(node['pattern'])
        if '$ref' in node:
            self._resolve(node['$ref'], resource)
        for keyword in _SCHEMA_VALUED:
            if keyword in node:
                self._check(node[keyword], resource, f'{where}/{keyword}')
        for keyword in _SCHEMA_LISTS:
            if keyword in node:
                if not isinstance(node[keyword], list) or (keyword != 'prefixItems' and not node[keyword]):
                    raise SchemaError(f'The {keyword} at {where or "/"} in {resource} must be a list of schemas.')
                for index, child in enumerate(node[keyword]):
                    self._check(child, resource, f'{where}/{keyword}/{index}')
        for keyword in _SCHEMA_MAPS:
            if keyword in node:
                if not isinstance(node[keyword], dict):
                    raise SchemaError(f'The {keyword} at {where or "/"} in {resource} must be an object.')
                for name, child in node[keyword].items():
                    self._check(child, resource, f'{where}/{keyword}/{_escape_pointer(name)}')

    def _resolve(self, reference, resource):
        """Find the schema a `$ref` names. Returns (schema, resource id)."""
        if not isinstance(reference, str):
            raise SchemaError(f'A $ref in {resource} must be a string.')
        uri, _, fragment = reference.partition('#')
        target_id = uri or resource
        if target_id not in self._schemas:
            raise SchemaError(f'The $ref {reference!r} in {resource} names a schema that is not registered.')
        node = self._schemas[target_id]
        if fragment and not fragment.startswith('/'):
            raise SchemaError(f'The $ref {reference!r} in {resource} uses an anchor, which is not supported.')
        if fragment:
            for segment in fragment[1:].split('/'):
                key = urllib.parse.unquote(segment).replace('~1', '/').replace('~0', '~')
                if isinstance(node, dict) and key in node:
                    node = node[key]
                elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                    node = node[int(key)]
                else:
                    raise SchemaError(f'The $ref {reference!r} in {resource} does not resolve.')
        return node, target_id

    # -- validation ------------------------------------------------------

    def validate(self, schema_id, instance):
        """Return the list of (pointer, message) errors. An empty list means valid. Raises KeyError for an unknown id."""
        if schema_id not in self._schemas:
            raise KeyError(schema_id)
        return self._node(self._schemas[schema_id], instance, '', schema_id)

    def _node(self, schema, value, path, resource):
        if schema is True:
            return []
        if schema is False:
            return [(path, 'boolean schema is false')]
        errors = []
        if '$ref' in schema:
            target, target_resource = self._resolve(schema['$ref'], resource)
            errors += self._node(target, value, path, target_resource)
        if 'type' in schema:
            names = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
            if not any(self._is_type(value, name) for name in names):
                errors.append((path, f"must be {' or '.join(names)}"))
        if 'enum' in schema and not any(_equal(value, option) for option in schema['enum']):
            errors.append((path, 'must be equal to one of the allowed values'))
        if 'const' in schema and not _equal(value, schema['const']):
            errors.append((path, 'must be equal to constant'))
        if isinstance(value, str):
            errors += self._string(schema, value, path)
        if _is_number(value):
            errors += self._number(schema, value, path)
        if isinstance(value, dict):
            errors += self._object(schema, value, path, resource)
        if isinstance(value, list):
            errors += self._array(schema, value, path, resource)
        errors += self._combinators(schema, value, path, resource)
        return errors

    @staticmethod
    def _is_type(value, name):
        if name == 'string':
            return isinstance(value, str)
        if name == 'number':
            return _is_number(value)
        if name == 'integer':
            return _is_integer(value)
        if name == 'boolean':
            return isinstance(value, bool)
        if name == 'null':
            return value is None
        if name == 'object':
            return isinstance(value, dict)
        return isinstance(value, list)

    def _string(self, schema, value, path):
        errors = []
        if 'minLength' in schema and len(value) < schema['minLength']:
            errors.append((path, f"must NOT have fewer than {schema['minLength']} characters"))
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            errors.append((path, f"must NOT have more than {schema['maxLength']} characters"))
        if 'pattern' in schema and not self._patterns[schema['pattern']].search(value):
            errors.append((path, f"must match pattern {schema['pattern']}"))
        return errors

    @staticmethod
    def _number(schema, value, path):
        errors = []
        if 'minimum' in schema and value < schema['minimum']:
            errors.append((path, f"must be >= {schema['minimum']}"))
        if 'maximum' in schema and value > schema['maximum']:
            errors.append((path, f"must be <= {schema['maximum']}"))
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            errors.append((path, f"must be > {schema['exclusiveMinimum']}"))
        if 'exclusiveMaximum' in schema and value >= schema['exclusiveMaximum']:
            errors.append((path, f"must be < {schema['exclusiveMaximum']}"))
        return errors

    def _object(self, schema, value, path, resource):
        errors = []
        if 'minProperties' in schema and len(value) < schema['minProperties']:
            errors.append((path, f"must NOT have fewer than {schema['minProperties']} properties"))
        if 'maxProperties' in schema and len(value) > schema['maxProperties']:
            errors.append((path, f"must NOT have more than {schema['maxProperties']} properties"))
        for name in schema.get('required', ()):
            if name not in value:
                errors.append((f'{path}/{_escape_pointer(name)}', f"must have required property '{name}'"))
        properties = schema.get('properties', {})
        if 'additionalProperties' in schema:
            extra = schema['additionalProperties']
            for name, child in value.items():
                if name in properties:
                    continue
                if extra is False:
                    errors.append((path, 'must NOT have additional properties'))
                else:
                    errors += self._node(extra, child, f'{path}/{_escape_pointer(name)}', resource)
        for name, sub in properties.items():
            if name in value:
                errors += self._node(sub, value[name], f'{path}/{_escape_pointer(name)}', resource)
        if 'propertyNames' in schema:
            for name in value:
                found = self._node(schema['propertyNames'], name, path, resource)
                if found:
                    errors += found
                    errors.append((path, f"property name '{name}' is invalid"))
        return errors

    def _array(self, schema, value, path, resource):
        errors = []
        if 'minItems' in schema and len(value) < schema['minItems']:
            errors.append((path, f"must NOT have fewer than {schema['minItems']} items"))
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            errors.append((path, f"must NOT have more than {schema['maxItems']} items"))
        if schema.get('uniqueItems') is True:
            seen = set()
            for item in value:
                key = _canonical(item)
                if key in seen:
                    errors.append((path, 'must NOT have duplicate items'))
                    break
                seen.add(key)
        prefix = schema.get('prefixItems', [])
        for index, sub in enumerate(prefix):
            if index < len(value):
                errors += self._node(sub, value[index], f'{path}/{index}', resource)
        if 'items' in schema:
            rest = schema['items']
            if rest is False:
                if len(value) > len(prefix):
                    errors.append((path, f'must NOT have more than {len(prefix)} items'))
            else:
                for index in range(len(prefix), len(value)):
                    errors += self._node(rest, value[index], f'{path}/{index}', resource)
        return errors

    def _combinators(self, schema, value, path, resource):
        errors = []
        for sub in schema.get('allOf', ()):
            errors += self._node(sub, value, path, resource)
        if 'anyOf' in schema:
            branches = [self._node(sub, value, path, resource) for sub in schema['anyOf']]
            if all(branches):
                errors += [error for branch in branches for error in branch]
                errors.append((path, 'must match a schema in anyOf'))
        if 'oneOf' in schema:
            branches = [self._node(sub, value, path, resource) for sub in schema['oneOf']]
            if sum(1 for branch in branches if not branch) != 1:
                errors += [error for branch in branches for error in branch]
                errors.append((path, 'must match exactly one schema in oneOf'))
        if 'not' in schema and not self._node(schema['not'], value, path, resource):
            errors.append((path, 'must NOT be valid'))
        if 'if' in schema:
            matched = not self._node(schema['if'], value, path, resource)
            clause = 'then' if matched else 'else'
            if clause in schema:
                found = self._node(schema[clause], value, path, resource)
                if found:
                    errors += found
                    errors.append((path, f'must match "{clause}" schema'))
        return errors


def load_contract_registry(directory=CONTRACTS_DIRECTORY):
    """Build a registry of the schemas that the lesson contracts need, from a folder of `*.schema.json` files.

    Only the three lesson kinds and the schemas they reach through `$ref` are
    loaded, so a schema for another contract never affects this validator.
    """
    by_id = {}
    for file in sorted(Path(directory).glob('*.schema.json')):
        try:
            schema = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise SchemaError(f'The schema file {file.name} cannot be read as JSON.') from error
        if not isinstance(schema, dict) or not isinstance(schema.get('$id'), str) or not schema['$id']:
            raise SchemaError(f'The schema file {file.name} has no $id.')
        if schema['$id'] in by_id:
            raise SchemaError(f"The $id {schema['$id']!r} appears in more than one schema file.")
        by_id[schema['$id']] = schema
    wanted = [f'urn:schola:contract:{kind}:v1' for kind in LESSON_CONTRACT_KINDS]
    chosen = {}
    while wanted:
        schema_id = wanted.pop()
        if schema_id in chosen:
            continue
        if schema_id not in by_id:
            raise SchemaError(f'The schema {schema_id} is missing from the contracts folder.')
        chosen[schema_id] = by_id[schema_id]
        wanted.extend(_referenced_ids(by_id[schema_id], schema_id))
    return SchemaRegistry(list(chosen.values()))


def _referenced_ids(node, own_id):
    found = []
    if isinstance(node, dict):
        reference = node.get('$ref')
        if isinstance(reference, str) and reference.partition('#')[0]:
            found.append(reference.partition('#')[0])
        for key, child in node.items():
            if key not in ('enum', 'const'):
                found.extend(_referenced_ids(child, own_id))
    elif isinstance(node, list):
        for child in node:
            found.extend(_referenced_ids(child, own_id))
    return [schema_id for schema_id in found if schema_id != own_id]


@functools.lru_cache(maxsize=1)
def contract_registry():
    """The registry for the real contract files next to this module."""
    return load_contract_registry(CONTRACTS_DIRECTORY)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _invalid(errors):
    if len(errors) <= MAX_ERRORS:
        return {'ok': False, 'reason': 'invalid', 'errors': errors}
    omitted = len(errors) - MAX_ERRORS
    return {'ok': False, 'reason': 'invalid', 'errors': errors[:MAX_ERRORS] + [{'pointer': '', 'message': f'{omitted} more errors omitted.'}]}


def _too_large():
    return {'ok': False, 'reason': 'too_large', 'errors': [{'pointer': '', 'message': 'The document is larger than the 5 MB limit.'}]}


def _unreadable(error):
    name = errno.errorcode.get(error.errno, 'unknown') if isinstance(error, OSError) and error.errno else 'unknown'
    return {'ok': False, 'reason': 'unreadable', 'errors': [{'pointer': '', 'message': f'The file could not be read ({name}).'}]}


def _error(pointer, message):
    return {'pointer': pointer, 'message': message}


def _pointer(*segments):
    return ''.join(f'/{_escape_pointer(segment)}' for segment in segments)


# ---------------------------------------------------------------------------
# Semantic checks: rules that a schema cannot state
# ---------------------------------------------------------------------------

def _days_in_month(year, month):
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _duplicates(items, key, base, errors, label):
    seen = set()
    for index, item in enumerate(items):
        if item[key] in seen:
            errors.append(_error(_pointer(*base, index, key), f'Each {label} must be unique. This one is repeated.'))
        seen.add(item[key])


def _module_errors(module):
    errors = []
    _duplicates(module['blocks'], 'id', ['blocks'], errors, 'block id')
    _duplicates(module['tasks'], 'id', ['tasks'], errors, 'task id')
    _duplicates(module['checks'], 'id', ['checks'], errors, 'check id')
    check_ids = {check['id'] for check in module['checks']}
    for index, block in enumerate(module['blocks']):
        if block['type'] == 'check-reference' and block['checkId'] not in check_ids:
            errors.append(_error(_pointer('blocks', index, 'checkId'), 'The block points at a check that this module does not declare.'))
    for index, task in enumerate(module['tasks']):
        hints = task['hints']
        if not (len(hints) == len(HINT_LADDER) and all(hint['level'] == HINT_LADDER[level] for level, hint in enumerate(hints))):
            errors.append(_error(
                _pointer('tasks', index, 'hints'),
                f"The hint ladder must hold exactly these levels in this order: {', '.join(HINT_LADDER)}."))
    for index, identifier in enumerate(module['prerequisites']):
        if identifier == module['id']:
            errors.append(_error(_pointer('prerequisites', index), 'A module cannot list itself as a prerequisite.'))
    errors.extend(_script_errors(module))
    return errors


_MINOR = re.compile(r'^1\.([0-9]+)$')
_LONE_SURROGATE = re.compile('[\ud800-\udfff]')
SCRIPT_FIELDS = ('runtime', 'source', 'args', 'timeoutSeconds')
# The largest script, in UTF-8 bytes. The schema counts characters, so the byte limit is checked here.
MAX_SCRIPT_BYTES = 65536


def _script_errors(module):
    """Rules for script checks (schema version 1.1) that the schema cannot state."""
    errors = []
    minor = int(_MINOR.match(module['schemaVersion']).group(1))
    if minor < 1:
        # Version 1.0 does not define the script fields. A 1.0 script runner with no fields stays valid.
        if any(field in check for check in module['checks'] for field in SCRIPT_FIELDS):
            errors.append(_error('/schemaVersion', 'Script check fields need schemaVersion 1.1 or later.'))
        return errors
    for index, check in enumerate(module['checks']):
        runner = check.get('runner')
        if isinstance(runner, dict) and runner.get('type') == 'script':
            for field in ('runtime', 'source'):
                if field not in check:
                    errors.append(_error(_pointer('checks', index, field), f'A script check needs the field {field}.'))
        source = check.get('source')
        if isinstance(source, str):
            if _LONE_SURROGATE.search(source):
                errors.append(_error(_pointer('checks', index, 'source'), 'The script holds a character that is not valid text.'))
            elif len(source.encode('utf-8')) > MAX_SCRIPT_BYTES:
                errors.append(_error(_pointer('checks', index, 'source'), f'The script is larger than {MAX_SCRIPT_BYTES} bytes.'))
    return errors


def _packet_errors(packet):
    errors = []
    _duplicates(packet['modules'], 'id', ['modules'], errors, 'module id')
    return errors


_STAMP_DAY = re.compile(r'^([0-9]{4})-([0-9]{2})-([0-9]{2})T')


def _help_record_errors(record):
    match = _STAMP_DAY.match(record['timestamp'])
    if match and int(match.group(3)) > _days_in_month(int(match.group(1)), int(match.group(2))):
        return [_error('/timestamp', 'The timestamp names a day that does not exist.')]
    return []


_SEMANTIC_CHECKS = {'lesson-module': _module_errors, 'packet': _packet_errors, 'help-record': _help_record_errors}


def semantic_errors(kind, document):
    """The semantic errors for a document that already passed its schema. Returns a list of error dicts."""
    return _SEMANTIC_CHECKS[kind](document)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

_MAJOR_MARKER = re.compile(r'^([0-9]+)\.')


def _is_supported_major(digits):
    stripped = digits.lstrip('0')
    # A longer number cannot be a supported major, and this avoids converting a huge digit string.
    return len(stripped) <= 9 and int(stripped or '0') in SUPPORTED_SCHEMA_MAJORS


def _run_document(kind, document):
    """Validate a parsed document. Returns (result, stage)."""
    if kind not in LESSON_CONTRACT_KINDS:
        return _invalid([_error('', f"Unknown contract kind. Use one of: {', '.join(LESSON_CONTRACT_KINDS)}.")]), 'kind'
    if not isinstance(document, dict):
        return _invalid([_error('', 'The document must be a JSON object.')]), 'document'
    # Read the leading major number first. A value such as 2.0.0 is an unsupported version, not a malformed one.
    marker = _MAJOR_MARKER.match(document['schemaVersion']) if isinstance(document.get('schemaVersion'), str) else None
    if marker and not _is_supported_major(marker.group(1)):
        message = f"This reader supports schema major version {', '.join(str(m) for m in SUPPORTED_SCHEMA_MAJORS)} only."
        return {'ok': False, 'reason': 'unsupported_version', 'errors': [_error('/schemaVersion', message)]}, 'version'
    found = contract_registry().validate(f'urn:schola:contract:{kind}:v1', document)
    if found:
        return _invalid([_error(pointer, message) for pointer, message in found]), 'schema'
    errors = semantic_errors(kind, document)
    if errors:
        return _invalid(errors), 'semantic'
    return {'ok': True}, 'none'


def validate_lesson_contract(kind, document):
    """Validate one parsed document. The document is read and never changed."""
    return _run_document(kind, document)[0]


def _reject_constant(name):
    raise ValueError(f'{name} is not allowed in JSON.')


def _no_repeats(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('An object repeats a key.')
    return dict(pairs)


def validate_lesson_text_with_stage(kind, text):
    """Like validate_lesson_text, and also names the stage that gave the result.

    The stage is `none`, `size`, `parse`, `kind`, `document`, `version`,
    `schema`, or `semantic`. Returns (result, stage).
    """
    if isinstance(text, (bytes, bytearray)):
        if len(text) > MAX_BYTES:
            return _too_large(), 'size'
        try:
            text = bytes(text).decode('utf-8')
        except UnicodeError:
            return _invalid([_error('', 'The text is not valid UTF-8.')]), 'parse'
    if not isinstance(text, str):
        return _invalid([_error('', 'The text must be a string.')]), 'parse'
    if len(text.encode('utf-8', errors='surrogatepass')) > MAX_BYTES:
        return _too_large(), 'size'
    try:
        document = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_repeats)
    except (ValueError, RecursionError):
        return _invalid([_error('', 'The text is not complete, valid JSON.')]), 'parse'
    try:
        return _run_document(kind, document)
    except RecursionError:
        return _invalid([_error('', 'The document is nested too deeply.')]), 'parse'


def validate_lesson_text(kind, text):
    """Parse JSON text, then validate it. Text over MAX_BYTES is refused before parsing."""
    return validate_lesson_text_with_stage(kind, text)[0]


def validate_lesson_file(kind, path):
    """Read a file and validate it. The file is opened for reading only.

    A file over MAX_BYTES is refused after a size check, before it is read.
    """
    try:
        info = os.stat(path)
        if info.st_size > MAX_BYTES:
            return _too_large()
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file')
        with open(path, 'rb') as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as error:
        return _unreadable(error)
    if len(data) > MAX_BYTES:
        return _too_large()
    return validate_lesson_text(kind, data)


def packet_from_module(module):
    """Build the one-entry packet that stands for a single lesson module. Validate the module first."""
    return {
        'schemaVersion': module['schemaVersion'],
        'id': module['id'],
        'version': module['version'],
        'title': module['title'],
        'modules': [{'id': module['id'], 'version': module['version']}],
    }
