import json
import pathlib
from collections import defaultdict

import yaml
from jsonschema.exceptions import ValidationError, UnknownType
# https://json-schema.org/blog/posts/bundling-json-schema-compound-documents
from jsonschema.validators import Draft202012Validator, validate

#
# check individual anaonymous schemas
#
for file_name in pathlib.Path('.').glob("*.yaml"):
    with open(file_name) as fp:
        Draft202012Validator.check_schema(yaml.load(fp, yaml.SafeLoader))

fhir_schema = json.load(open("aced-bmeg.json"))
Draft202012Validator.check_schema(fhir_schema)
fhir_validator = Draft202012Validator(fhir_schema)


def is_edge(schema) -> bool:
    """ Has an edge vocabulary?"""
    vocabulary_fields = [
        'source_property_name',
        'source_multiplicity',
        'destination_property_name',
        'destination_multiplicity',
        'label',
        'is_primary',
        'source_type',
        'destination_type',
    ]
    return all([f in schema for f in vocabulary_fields])


print("There are", len(fhir_schema['$defs']), " types.")
vertex_schemas = [v for v in fhir_schema['$defs'].values() if not is_edge(v)]
edge_schemas = [v for v in fhir_schema['$defs'].values() if is_edge(v)]
print("  ", len(vertex_schemas), " vertices.")
print("  ", len(edge_schemas), " edges.")

print('validation tests:')
#
# test all vertex types in the schema
#
for vertex_name in fhir_schema['$defs']:

    if not is_edge(fhir_schema['$defs'][vertex_name]):
        continue

    try:
        # a trivial instance example
        fhir_validator.validate(
            {"id": "1234", "resourceType": vertex_name},
        )
        # print(f'ok - {vertex_name} validated correctly')
    except ValidationError as e:
        # print(f'ok, expected error - {vertex_name}  {[c.message for c in e.context if c.schema["title"] == vertex_name]}')
        pass
    except UnknownType as e:
        print("!ok Houston we have a problem.  All types in $defs should have a schema.")
        raise e

#
# a negative test - all should fail
#
for vertex_name in fhir_schema['$defs']:

    if not is_edge(fhir_schema['$defs'][vertex_name]):
        continue

    try:
        # a trivial instance example
        fhir_validator.validate(
            {"id": 1234, "resourceType": vertex_name},
        )
        raise Exception(f'!ok - {vertex_name} should have raised ValidationError')
    except ValidationError as e:
        pass

print('  ok - caught invalid id')

#
# spot check an embedded "type"
#
try:
    instance = {"id": "1234", "resourceType": "HumanName", "period": {"start": "1234"}}
    assert not validate(instance, fhir_schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    print('  fail - fhir_schema period nonsense period invalid')
except ValidationError as e:
    assert '1234' in e.message
    print('  ok - fhir_schema period nonsense period invalid correctly')

try:
    instance = {"id": "1234", "resourceType": 'HumanName',
                "period": {"start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}}
    validate(instance, fhir_schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    # print('ok - fhir_schema period valid period validated correctly')
except ValidationError as e:
    print('  fail - fhir_schema valid period should have validated ', instance)

print(f'  ok - all vertices validated correctly')

fail = False
for edge in edge_schemas:
    try:
        fhir_validator.validate(
            {
                "resourceType": edge['title'],
                "source": {"reference": "Patient/1234"},
                "target": {"reference": "Specimen/5678"},
            }
        )
    except ValidationError as e:
        fail = True
        print('  fail - fhir_schema should have validated edge with relative References', edge['title'])
    # print(f"ok {edge['title']}")
if not fail:
    print(f'  ok - all edges validated correctly with relative References')

fail = False
for edge in edge_schemas:
    try:
        fhir_validator.validate(
            {
                "resourceType": edge['title'],
                "source": {"type": "Patient", "identifier": {"value": "1234"}},
                "target": {"type": "Specimen", "identifier": {"value": "5678"}},
            }
        )
    except ValidationError as e:
        fail = True
        print('fail - fhir_schema should have validated granular Reference', edge['title'])
    # print(f"ok {edge['title']}")
if not fail:
    print(f'  ok - all edges validated correctly with type/identifier References')

invalid_edges = [
    # missing resourceType
    {
        "source": {"type": "Patient", "identifier": {"value": "1234"}},
        "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
    },
    # missing source
    {
        "resourceType": 'Observation_performer_Patient',
        "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
    },
    # missing destination
    {
        "resourceType": 'Observation_performer_Patient',
        "source": {"type": "Patient", "identifier": {"value": "1234"}},
    },
    # invalid source reference
    {
        "resourceType": 'Observation_performer_Patient',
        "source": {"foo"},
        "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
    },
    # invalid destination reference
    {
        "resourceType": 'Observation_performer_Patient',
        "source": {"type": "Patient", "identifier": {"value": "1234"}},
        "destination": {"foo"},
    },
]

for edge in invalid_edges:
    try:
        fhir_validator.validate(edge)
        assert False, ("Should not validate", edge)
    except ValidationError as e:
        pass

print(f'  ok - all invalid edges validated correctly')


def edge_validator(validator: Draft202012Validator, edge):
    """Extra layer of validation, check both schema validation and vocabulary matching."""
    validator.validate(edge)
    sub_schema = validator.schema['$defs'][edge['resourceType']]
    if is_edge(sub_schema):
        expected_source_type = sub_schema['source_type']
        expected_destination_type = sub_schema['destination_type']
        source = edge['source']
        destination = edge['target']
        if 'reference' in source:
            actual_source_type = source['reference'].split('/')[0]
        else:
            assert 'type' in source, ('Edge.source needs reference or source', edge)
            actual_source_type = source['type']
        if 'reference' in destination:
            actual_destination_type = destination['reference'].split('/')[0]
        else:
            assert 'type' in destination, ('Edge.destination needs reference or source', edge)
            actual_destination_type = destination['type']
        if not actual_source_type == expected_source_type:
            raise ValidationError(f"{actual_source_type} does not match expected {expected_source_type}")
        if not actual_destination_type == expected_destination_type:
            raise ValidationError(f"{actual_destination_type} does not match expected {expected_destination_type}")


for edge in edge_schemas:
    try:
        edge_validator(fhir_validator, {
            "resourceType": edge['title'],
            "source": {"type": "XXX", "identifier": {"value": "1234"}},
            "destination": {"type": "YYYY", "identifier": {"value": "5678"}},
        })
        assert False, ("Should not validate", edge)
    except ValidationError as e:
        pass
print('  ok all invalid edges pass "extra" vocabulary validation')

fail = False
for edge in edge_schemas:
    instance = {
            "resourceType": edge['title'],
            "source": {"type": edge['source_type'], "identifier": {"value": "1234"}},
            "target": {"type": edge['destination_type'], "identifier": {"value": "5678"}},
        }
    try:
        edge_validator(fhir_validator, instance)
    except ValidationError as e:
        fail = True
        print(f"fail extra edge_validator {instance}")
if not fail:
    print('  ok all valid edges pass "extra" vocabulary validation')

print(f'count of primary edges:\n  ', len([edge for edge in edge_schemas if edge['is_primary']]))

print('unresolved `Any` edge destinations:')
needs_unresolved_primary = False
for edge in edge_schemas:
    if edge['destination_type'] == 'Resource' and len([e for e in edge_schemas if e['source_type'] == edge['source_type']]) == 1:
        print('  ', edge['source_type']+'.'+edge['source_property_name'], edge['destination_type'])
        needs_unresolved_primary = True
if not needs_unresolved_primary:
    print('  None')

print('multi-edges (ie need `is_primary` manually set for uni-graphs (gen3)):')

edge_counts = defaultdict(dict)
for edge in edge_schemas:
    if edge['destination_type'] not in edge_counts[edge['source_type']]:
        edge_counts[edge['source_type']][edge['destination_type']] = False
    if not edge_counts[edge['source_type']][edge['destination_type']]:
        edge_counts[edge['source_type']][edge['destination_type']] = edge['is_primary']

needs_is_primary = False
for source_type in edge_counts:
    for destination_type in edge_counts[source_type]:
        if not edge_counts[source_type][destination_type]:
            if destination_type == 'Resource':
                continue
            print(source_type, '->', destination_type)
            needs_is_primary = True

if not needs_is_primary:
    print('  None')

tab = '\t'

# with open('../docs/edges.tsv', 'w') as fp:
#     for edge_title in sorted([edge['title'] for edge in edge_schemas]):
#         fp.write(tab.join(edge_title.split('_')))
#         fp.write('\n')
# print('cytoscape friendly tsv written to ../docs/edges.tsv')


# print('Observation properties')
# for k, p in fhir_schema['$defs']['Observation']['properties'].items():
#     if 'one_of_many' in p:
#         if '$ref' in p:
#             type_ = fhir_schema['$defs'][p['$ref'].split('/')[-1]]
#             print(k)
#             for sk in type_['properties']:
#                 if sk in ['extension', 'fhir_comments', 'id', 'resourceType'] or sk.startswith('_'):
#                     continue
#                 print('  '+sk)
#         else:
#             print(k + '\n  ' + p['type'])
