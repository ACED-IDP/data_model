import json
from collections import defaultdict

from jsonschema.exceptions import ValidationError, UnknownType
# https://json-schema.org/blog/posts/bundling-json-schema-compound-documents
from jsonschema.validators import Draft202012Validator, validate

fhir_schema = json.load(open("coherent.json"))
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

#
# test all vertex types in the schema
#
for vertex_name in fhir_schema['$defs']:

    if not is_edge(fhir_schema['$defs'][vertex_name]):
        continue

    try:
        # a trivial instance example
        fhir_validator.validate(
            {"id": "1234", "resource_type": vertex_name},
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
            {"id": 1234, "resource_type": vertex_name},
        )
        print(f'!ok - {vertex_name} should have raised ValidationError')
    except ValidationError as e:
        pass

#
# spot check an embedded "type"
#
try:
    instance = {"id": "1234", "resource_type": "HumanName", "period": {"start": "1234"}}
    assert not validate(instance, fhir_schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    print('fail - fhir_schema period nonsense period invalid')
except ValidationError as e:
    assert '1234' in e.message
    # print('ok - fhir_schema period nonsense period invalid correctly')

try:
    instance = {"id": "1234", "resource_type": 'HumanName',
                "period": {"start": "2023-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"}}
    validate(instance, fhir_schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    # print('ok - fhir_schema period valid period validated correctly')
except ValidationError as e:
    print('fail - fhir_schema valid period should have validated ' + str(e))

print(f'ok - all vertices validated correctly')

for edge in edge_schemas:
    fhir_validator.validate(
        {
            "resource_type": edge['title'],
            "source": {"reference": "Patient/1234"},
            "destination": {"reference": "Specimen/5678"},
        }
    )
print(f'ok - all edges validated correctly with relative References')


for edge in edge_schemas:
    fhir_validator.validate(
        {
            "resource_type": edge['title'],
            "source": {"type": "Patient", "identifier": {"value": "1234"}},
            "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
        }
    )
print(f'ok - all edges validated correctly with type/identifier References')

invalid_edges = [
    # missing resource_type
    {
        "source": {"type": "Patient", "identifier": {"value": "1234"}},
        "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
    },
    # missing source
    {
        "resource_type": 'Observation_performer_Patient',
        "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
    },
    # missing destination
    {
        "resource_type": 'Observation_performer_Patient',
        "source": {"type": "Patient", "identifier": {"value": "1234"}},
    },
    # invalid source reference
    {
        "resource_type": 'Observation_performer_Patient',
        "source": {"foo"},
        "destination": {"type": "Specimen", "identifier": {"value": "5678"}},
    },
    # invalid destination reference
    {
        "resource_type": 'Observation_performer_Patient',
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

print(f'ok - all invalid edges validated correctly')


def edge_validator(validator: Draft202012Validator, edge):
    """Extra layer of validation, check both schema validation and vocabulary matching."""
    validator.validate(edge)
    sub_schema = validator.schema['$defs'][edge['resource_type']]
    if is_edge(sub_schema):
        expected_source_type = sub_schema['source_type']
        expected_destination_type = sub_schema['destination_type']
        source = edge['source']
        destination = edge['destination']
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
            "resource_type": edge['title'],
            "source": {"type": "XXX", "identifier": {"value": "1234"}},
            "destination": {"type": "YYYY", "identifier": {"value": "5678"}},
        })
        assert False, ("Should not validate", edge)
    except ValidationError as e:
        pass
print('ok all invalid edges pass "extra" vocabulary validation')

for edge in edge_schemas:
    edge_validator(fhir_validator, {
        "resource_type": edge['title'],
        "source": {"type": edge['source_type'], "identifier": {"value": "1234"}},
        "destination": {"type": edge['destination_type'], "identifier": {"value": "5678"}},
    })
print('ok all valid edges pass "extra" vocabulary validation')

print(f'count of primary edges', len([edge for edge in edge_schemas if edge['is_primary']]))

print('multi-edges (ie need `is_primary` manually set for uni-graphs (gen3))')

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
            print(source_type, '->', destination_type)
            needs_is_primary = True

if not needs_is_primary:
    print('  None')

tab = '\t'

with open('../docs/edges.tsv', 'w') as fp:
    for edge_title in sorted([edge['title'] for edge in edge_schemas]):
        fp.write(tab.join(edge_title.split('_')))
        fp.write('\n')
print('cytoscape friendly tsv written to ../docs/edges.tsv')

