import csv
import importlib
import json
import logging
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from typing import TextIO, Optional, List

import click
import pathlib

import inflection
from fhir.resources import FHIRAbstractModel
from fhir.resources.bundle import Bundle
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.core.utils.common import normalize_fhir_type_class, get_fhir_type_name
from fhir.resources.fhirprimitiveextension import FHIRPrimitiveExtension
from pydantic import ValidationError
from yaml import SafeLoader
import yaml
from fhir.resources.reference import Reference
from flatten_json import flatten as flatten_dict
from dataclass_csv import DataclassWriter

import orjson

from gen3.metadata import Gen3Metadata
from gen3.submission import Gen3Submission


# thread local instance for our Reference renderer
LINKS = threading.local()
CLASSES = threading.local()
IDENTIFIER_LIST_SIZE = 8

ACED_NAMESPACE = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced-ipd.org')


@dataclass
class EdgeInfo:
    destination_type: str
    """Destination Vertex"""
    source_type: str
    """Source Vertex"""
    source_property_name: str
    """Source property"""
    source_docstring: Optional[str] = None
    """Docstring for source class"""
    source_property_docstring: Optional[str] = None
    """Docstring for property in source class."""
    backref: Optional[str] = None
    """Destination property"""


@dataclass
class BackRefAnalysis:
    destinations: List[str]
    """Unique list of destination vertex names."""

    sources: List[str]
    """Unique list of source vertex names."""

    edges: List[EdgeInfo]
    """Unique list of source vertex names."""


def _bundle_schemas(output_path, base_uri):
    """Create a single uber schema in json with all """
    schemas = {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': base_uri,
        '$defs': {},
        'anyOf': []
    }

    for input_file in output_path.glob('*.yaml'):

        if input_file.stem.startswith('_'):
            continue

        with open(input_file) as fp:
            schema_str = fp.read()
            schema_str = schema_str.replace('.yaml', '').replace('$ref: ', f'$ref: {base_uri}/')
            schema = yaml.safe_load(schema_str)

            if 'title' not in schema:
                vertex = next(
                    iter([k for k in schema.keys() if not k.startswith('$') and k not in ['description', 'allOf']]), None)
                _ = schema
                schema = schema[vertex]

            assert 'title' in schema, schema
            schema['$id'] = f"{base_uri}/{schema['title']}"

            schemas['$defs'][schema['title']] = schema
            schemas['anyOf'].append({'$ref': f"{base_uri}/{schema['title']}"})

    with open(output_path / pathlib.Path("aced-bmeg.json"), "w") as fp:
        json.dump(schemas, fp, indent=2)

    print(f"Aggregated schema written to {output_path / pathlib.Path('aced-bmeg.json')}")


@click.group()
def cli():
    pass


@cli.command('generate')
@click.option('--output_path', required=True,
              default=None,
              show_default=True,
              help='Path to generated schema files'
              )
@click.option('--config_path',
              default='gen3.config.yaml',
              show_default=True,
              help='Path to gen3 config file.')
@click.option('--gen3_fixtures',
              default='static_gen3_fixtures',
              show_default=True,
              help='Path to gen3 static data dictionary files.')
def bundle_schema(output_path, config_path, gen3_fixtures):
    """Create BMEG and gen3 schemas"""

    output_path = pathlib.Path(output_path)
    config_path = pathlib.Path(config_path)
    gen3_fixtures = pathlib.Path(gen3_fixtures)
    assert output_path.is_dir()
    assert gen3_fixtures.is_dir()
    assert config_path.is_file()
    with open(config_path) as fp:
        gen3_config = yaml.load(fp, SafeLoader)

    # our namespace
    base_uri = 'http://bmeg.io/schema/0.0.1'
    # not gen3 boilerplate
    class_names = [c for c in gen3_config['dependency_order'] if not c.startswith('_')]
    class_names = [c for c in class_names if c not in ['Program', 'Project']]
    mod = importlib.import_module('fhir.resources')
    classes = set()
    for class_name in class_names:
        classes.add(mod.get_fhir_model_class(class_name))

    # find subclasses 3 levels deep
    for _ in range(3):
        embedded_classes = set()
        for klass in classes:
            for p in klass.element_properties():
                mod = importlib.import_module('fhir.resources')
                try:
                    embedded_class = mod.get_fhir_model_class(get_fhir_type_name(p.type_))
                    embedded_classes.add(embedded_class)
                except KeyError:
                    pass
        classes.update(embedded_classes)
        classes.add(FHIRPrimitiveExtension)
    # print('classes found after 2 iterations', classes)
    edges, edge_names = _extract_edge_info(classes, gen3_config['dependency_order'])
    edge_names = set(edge_names)
    incoming_edges = _filter_incoming_edges(edges)

    schemas = {}

    for klass in classes:
        schema = klass.schema()
        _add_reference_backrefs(incoming_edges, schema, gen3_config['manually_curated_edges'])
        schema['description'] = schema.get('description', '').replace('\n\n', '\n')

        # rename python style name back to resourceType
        assert 'resource_type' in schema['properties'], schema
        schema['properties']['resourceType'] = schema['properties']['resource_type']
        del schema['properties']['resource_type']

        #
        # style as an anonymous schema
        #

        # rename type Resource to $ref
        for p_ in schema['properties'].values():
            if 'type' not in p_:
                continue
            if p_['type'][0].isupper():
                p_['$ref'] = f"{p_['type']}.yaml"
                del p_['type']
        for p_ in schema['properties'].values():
            if 'items' not in p_:
                continue
            if p_['items']['type'][0].isupper():
                p_['items']['$ref'] = f"{p_['items']['type']}.yaml"
                del p_['items']['type']

        # rename $id to id
        schema['id'] = schema['$id']
        del schema['$id']

        with open(output_path / pathlib.Path(klass.__name__ + ".yaml"), "w") as fp:
            # yaml.dump({
            #     "$schema": "https://json-schema.org/draft/2020-12/schema",
            #     "$id": f"{base_uri}/{klass.__name__}",
            #     klass.__name__: schema
            # }, fp)
            yaml.dump(schema, fp)

        schemas[klass.__name__] = schema
    print('Individual Vertex schemas written to', output_path)

    # used to automatically set 'is_primary'
    edge_counts = defaultdict(dict)
    for edge_info in edges:
        if edge_info.destination_type not in edge_counts[edge_info.source_type]:
            edge_counts[edge_info.source_type][edge_info.destination_type] = 0
        edge_counts[edge_info.source_type][edge_info.destination_type] += 1

    edge_schemas = {}
    for edge_info in edges:

        title = f"{edge_info.source_type}_{edge_info.source_property_name}_{edge_info.destination_type}"
        is_primary = edge_counts[edge_info.source_type][edge_info.destination_type] == 1
        if not is_primary and edge_info.source_property_name == 'subject':
            is_primary = True

        edge_config_template = f"""
        "$id": {title}
        description: {edge_info.source_property_docstring}. (Generated Edge)
        # start vocabulary fields
        source_property_name: {edge_info.source_property_name}
        source_multiplicity: many
        destination_property_name: {edge_info.backref}
        destination_multiplicity: many
        label: {title}
        is_primary: {is_primary}
        source_type: {edge_info.source_type}
        destination_type: {edge_info.destination_type}
        # end vocabulary fields
        type: object
        title: {title}
        allOf:
        - "$ref": EdgeConfig.yaml   
        """
        schema = yaml.safe_load(edge_config_template)
        with open(output_path / pathlib.Path(f"Edge_{title}" + ".yaml"), "w") as fp:
            yaml.dump({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{base_uri}/{title}",
                title: schema
            }, fp)
        edge_schemas[title] = schema

    # write EdgeConfig and manually configured edges
    # read from config apply context $base_uri
    for title, edge_config in gen3_config['manually_curated_edges'].items():
        schema = json.loads(json.dumps(edge_config).replace('$base_uri', base_uri))
        with open(output_path / pathlib.Path(f"Edge_{title}.yaml"), "w") as fp:
            yaml.dump({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{base_uri}/{title}",
                title: schema
            }, fp)

        edge_schemas[title] = schema

    print('Individual Edge schemas written to', output_path)
    _bundle_schemas(output_path, base_uri)

    _gen3_schemas(gen3_config, output_path, schemas, gen3_fixtures, edge_schemas)


@cli.command(name='publish')
@click.argument('dictionary_path', default='generated-json-schema/aced.json')
@click.option('--bucket', default="s3://aced-public", help="Bucket target", show_default=True)
@click.option('--production', default=False, is_flag=True, show_default=True,
              help="Write to aced.json, otherwise aced-test.json")
def schema_publish(dictionary_path, bucket, production):
    """Copy dictionary to s3 (note:aws cli dependency)"""

    dictionary_path = pathlib.Path(dictionary_path)
    assert dictionary_path.is_file(), f"{dictionary_path} should be a path"
    click.echo(f"Writing schema into {bucket}")
    import subprocess
    if production:
        cmd = f"aws s3 cp {dictionary_path} {bucket}".split(' ')
    else:
        cmd = f"aws s3 cp {dictionary_path} {bucket}/aced-test.json".split(' ')
    s3_cp = subprocess.run(cmd)
    assert s3_cp.returncode == 0, s3_cp
    print("OK")


def _filter_incoming_edges(edges):
    # order it by dst str
    incoming_edges = defaultdict(dict)
    for edge in edges:
        if edge.source_type not in incoming_edges[edge.destination_type]:
            incoming_edges[edge.destination_type][edge.source_type] = {'count': 0, 'backrefs': [], 'docstrings': {}}
        incoming_edges[edge.destination_type][edge.source_type]['count'] += 1
    # loop through edges again, determine backref
    for edge in edges:
        backref = inflection.underscore(edge.source_type)
        if incoming_edges[edge.destination_type][edge.source_type]['count'] > 1:
            backref = f"{edge.source_property_name}_{inflection.underscore(edge.source_type)}"
        incoming_edges[edge.destination_type][edge.source_type]['backrefs'].append(backref)
        incoming_edges[edge.destination_type][edge.source_type]['docstrings'][backref] = edge.source_property_docstring
        edge.backref = backref
    return incoming_edges


def _extract_edge_info(classes, desired_vertices):
    edges = []
    edge_names = []

    for klass in classes:
        # if klass.__name__ not in desired_vertices:
        #     continue
        if not hasattr(klass, '__fields__'):
            continue
        if hasattr(klass, 'is_primitive') and klass.is_primitive():
            continue

        for k, v in klass.__fields__.items():
            if 'enum_reference_types' in v.field_info.extra:
                for enum_reference_type in set(v.field_info.extra['enum_reference_types']):
                    if enum_reference_type not in desired_vertices:
                        if v.field_info.description and 'Note:' not in v.field_info.description:
                            v.field_info.description += \
                                f" Note: following not in scope, see config.dependency_order."
                        if v.field_info.description and str(enum_reference_type) not in v.field_info.description:
                            v.field_info.description += f" {enum_reference_type}"
                        logging.getLogger(__name__).warning(f"{klass.__name__}.{k} {enum_reference_type} not in scope.")
                        continue
                    edge_info = EdgeInfo(**{
                        'destination_type': enum_reference_type,
                        'source_type': klass.__name__,
                        'source_docstring': klass.__doc__.replace('\n', ' ').replace('  ', ' ').replace('  ',
                                                                                                        ' ').strip(),
                        'source_property_name': k,
                        'source_property_docstring': v.field_info.title
                    })
                    edges.append(edge_info)
                    edge_names.append(klass.__name__)

    return edges, edge_names


def _add_reference_backrefs(incoming_edges, schema, manually_curated_edges):
    """Set the backref"""
    schema['$id'] = schema['title']
    for k, p in schema['properties'].items():
        if 'enum_reference_types' not in p:
            continue

        src_name = schema['title']

        edge_config = next(iter([edge_config for edge_label, edge_config in manually_curated_edges.items() if
                                 edge_label.startswith(f"{src_name}_{k}")]), None)

        reference_backrefs = {}
        for dst_name in p['enum_reference_types']:
            if dst_name not in incoming_edges:
                continue
            if src_name not in incoming_edges[dst_name]:
                continue
            check_property_name = len(incoming_edges[dst_name][src_name]['backrefs']) > 1
            for backref in incoming_edges[dst_name][src_name]['backrefs']:
                if check_property_name and backref.startswith(k):
                    reference_backrefs[dst_name] = backref
                elif not check_property_name:
                    reference_backrefs[dst_name] = backref
        targets = []
        if len(reference_backrefs) == 0 and edge_config:
            # logging.getLogger(__name__).warning(('no reference_backrefs w/ config', f"{schema['title']}.{k}", edge_config))
            reference_backrefs[edge_config['destination_type']] = edge_config['destination_property_name']
        if len(reference_backrefs) == 0 and not edge_config and 'Resource' in p['enum_reference_types']:
            logging.getLogger(__name__).warning(('Any reference', f"{schema['title']}.{k}", p['enum_reference_types']))
        if len(reference_backrefs) == 0 and not edge_config and 'Resource' not in p['enum_reference_types']:
            logging.getLogger(__name__).warning(('Out of scope reference', f"{schema['title']}.{k}", p['enum_reference_types']))
        if len(reference_backrefs) > 1:
            logging.getLogger(__name__).info(
                ('multiple reference_backrefs', f"{schema['title']}.{k}", reference_backrefs))
        if len(reference_backrefs) > 0:
            for backref_type, backref in reference_backrefs.items():
                targets.append(
                    {
                        'type': {'$ref': f"{backref_type}.yaml"},
                        'backref': backref
                    }
                )
        if len(targets) > 0:
            p['targets'] = targets
        del p['enum_reference_types']

        # TODO - review: somehow the python fieldinfo for properties that are for_many Reference and CodeableConcept
        # contain binding_* fields in the Reference property?  remove for now
        for _ in ['binding_description', 'binding_strength', 'binding_uri']:
            if _ in p:
                del p[_]


def _gen3_schemas(gen3_config, output_path, schemas, gen3_fixtures, edge_schemas):
    """xform for gen3."""

    # config_paths = gen3_config['paths']
    config_categories = gen3_config['categories']
    ignored_properties = gen3_config['ignored_properties']
    dependency_order = gen3_config['dependency_order']
    # embedded_ignored_properties = gen3_config['embedded_ignored_properties']
    needs_to_string = set()

    # process vertex schema for gen3
    linked_vertices = set()
    for k, schema in schemas.items():
        if 'properties' not in schema:
            # print(f"No properties in {k}")
            continue

        # add boiler plate
        schema["program"] = "*"
        schema["project"] = "*"
        schema["category"] = config_categories.get(schema['title'], 'Clinical')

        # rename $id to id, make it lowercase since Peregrine does not quote table names
        schema['id'] = inflection.underscore(schema['id'])

        # add links
        src_name = schema['title']
        links = []

        for title, edge in edge_schemas.items():
            if not title.startswith(schema['title'] + '_'):
                continue
            if not edge['is_primary']:
                continue

            # gen3 uses source_property_name as a PK. Dups will be silently dropped.
            source_property_name = edge['source_property_name']
            if edge['destination_type'] != 'Patient' and source_property_name == 'subject':
                source_property_name = f"subject_{edge['destination_type']}"

            links.append({
                'backref': inflection.underscore(edge['destination_property_name']),
                'label': edge['label'],
                'multiplicity': 'many_to_many',
                'name': source_property_name,
                'required': False,
                'target_type': inflection.underscore(edge['destination_type'])
            })

            linked_vertices.add(edge['destination_type'])
            linked_vertices.add(edge['source_type'])

        # assign links to vertex
        schema["links"] = links

        # simplify. Brute force, cast complex types to string
        stringified_types = ['Coding', 'CodeableConcept', 'Identifier']
        properties_to_delete = []
        properties_to_add = []  # for re-cast lists
        for name_, property_ in schema['properties'].items():
            property_type = None
            if '$ref' in property_:
                property_type = property_['$ref'].split('.')[0]
            if not property_type and 'type' in property_:
                property_type = property_['type'].split('.')[0]
            if not property_type:
                properties_to_delete.append(name_)
                continue
            elif name_ in ignored_properties:
                properties_to_delete.append(name_)
                continue
            elif property_type in ['array']:
                if 'description' in property_:
                    item_type = None
                    if '$ref' in property_['items']:
                        item_type = property_['items']['$ref']
                    else:
                        assert 'type' in property_['items'], property_['items']
                        item_type = property_['items']['type']
                    assert item_type, property_
                    property_['description'] += f" (array: {item_type}"
                print(schema['title'], name_, property_['items'], "list")
                property_['type'] = 'array'
                property_['items'] = {
                    "type": "string"
                }
                continue
            elif 'one_of_many' in property_ and property_type in schemas:
                if property_type in ['CodeableConcept']:
                    properties_to_add.append((f"{name_}_coding", {'type': 'string', 'title': "Coded representation."}))
                if property_type in ['Quantity']:
                    properties_to_add.append((f"{name_}_unit", {'type': 'string', 'title': "Unit representation."}))
                    properties_to_add.append(
                        (f"{name_}_value", {'type': 'number', 'title': "Numerical value (with implicit precision)"}))
                stringified_types.append(property_type)
                property_['type'] = 'string'
                needs_to_string.add(property_type)

            elif property_type in ['CodeableConcept']:
                properties_to_add.append((f"{name_}_coding", {'type': 'string', 'title': "Coded representation."}))

            elif property_type in ['Quantity']:
                properties_to_add.append((f"{name_}_unit", {'type': 'string', 'title': "Unit representation."}))
                properties_to_add.append(
                    (f"{name_}_value", {'type': 'number', 'title': "Numerical value (with implicit precision)"}))

            # TODO - delete me?
            elif property_type[0].isupper() and property_type not in stringified_types:
                properties_to_delete.append(name_)
                continue

        for name_ in properties_to_delete:
            del schema['properties'][name_]

        for name_, property_ in properties_to_add:
            schema['properties'][name_] = property_

        # simplified property types, cast to string
        for name_, property_ in schema['properties'].items():
            property_type = None
            if '$ref' in property_:
                property_type = property_['$ref'].split('.')[0]
            if not property_type and 'type' in property_:
                property_type = property_['type'].split('.')[0]
            if property_type in stringified_types:
                if 'description' in property_:
                    property_['description'] += f" {property_type}"
                property_['type'] = 'string'
                del property_['$ref']

        # add termDefs
        for name_, property_ in schema['properties'].items():
            if 'binding_strength' in property_ and 'binding_uri' in property_:
                property_['term'] = {
                    'description': property_.get('binding_description', ''),
                    'termDef': {
                        'cde_id': property_['binding_uri'],
                        'term': property_['binding_uri'],
                        'term_url': property_['binding_uri'],
                        'cde_version': None,
                        'source': 'fhir',
                        'strength': property_['binding_strength']
                    }
                }

        extra_properties = gen3_config['extra_properties']
        if schema['title'] in extra_properties:
            for snippet_key, snippet_value in extra_properties[schema['title']].items():
                schema['properties'][snippet_key] = snippet_value

        # add gen3 scaffolding
        schema['properties']['project_id'] = {'$ref': '_definitions.yaml#/project_id'}

        # simplified property types, cast to string
        for name_, property_ in schema['properties'].items():
            if 'targets' in property_:
                del property_['targets']

        # sort properties for readability
        _ = sorted(schema['properties'].keys())
        schema['properties'] = {k: schema['properties'][k] for k in _}

        # manage required
        if 'required' in schema:
            required_link_names = []
            # if a link is required, mark it so
            for required in schema['required']:
                required_link = next(iter([l_ for l_ in schema['links'] if l_['name'] == required]), None)
                if required_link:
                    required_link['required'] = True
                    required_link_names.append(required)
            # and delete it from required properties
            for _ in required_link_names:
                schema['required'].remove(_)

    # delete vertices we don't want
    vertices_to_delete = set(schemas.keys()) - set(dependency_order)
    for k in vertices_to_delete:
        del schemas[k]

    # add gen3 scaffolding
    for fn in ["_definitions.yaml", "_terms.yaml", "_program.yaml", "_project.yaml", "_core_metadata_collection.yaml",
               "_settings.yaml"]:
        with open(gen3_fixtures / pathlib.Path(fn)) as fp:
            schemas[fn] = yaml.load(fp, SafeLoader)

    # save gen3 schema in order
    dependency_order.extend(k for k in schemas if k not in dependency_order)
    with open(output_path / pathlib.Path("aced.json"), "w") as fp:
        # change to lowercase for gen3
        json.dump({inflection.underscore(k): schemas[k] for k in dependency_order if k in schemas}, fp,  indent=2)
    print('Gen3 schemas written to', output_path / pathlib.Path("aced.json"))
    # print('needs_to_string', needs_to_string)
    #
    # done with gen3 xform
    #


def is_edge(schema) -> bool:
    """jsonschema type has an edge vocabulary?"""
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


@cli.command('cytoscape')
@click.option('--input_path', required=True,
              default='generate-json-schema/coherent.json',
              show_default=True,
              help='Path to schema files'
              )
@click.option('--output_path', required=True,
              default=None,
              show_default=True,
              help='Path to cytoscape files'
              )
def bundle_cytoscape(input_path, output_path):
    """Extract a SIF file for import into cytoscape."""

    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    assert input_path.is_file()
    assert output_path.is_dir()

    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    assert input_path.is_file()
    assert output_path.is_dir()

    with open(input_path) as fp:
        schema = json.load(fp)

    edges = [(name, edge) for name, edge in schema["$defs"].items() if is_edge(edge)]

    fn = output_path / pathlib.Path('edges.sif.inbound.tsv')
    with open(fn, 'w', newline='') as tsv_file:
        writer = csv.writer(tsv_file, delimiter='\t', lineterminator='\n')
        writer.writerow(['sourceName', '(edgeType)', 'targetName'])
        # for edge in edges:
        #     writer.writerow([edge['destination_type'], edge['destination_property_name'], edge['source_type'])

        for name, edge in edges:
            writer.writerow([edge['source_type'], edge['source_property_name'], edge['destination_type']])

    print(f'edges written to {fn}')

    fn = output_path / pathlib.Path('edges.sif.primary.tsv')
    with open(fn, 'w', newline='') as tsv_file:
        writer = csv.writer(tsv_file, delimiter='\t', lineterminator='\n')
        writer.writerow(['sourceName', '(edgeType)', 'targetName'])

        for name, edge in edges:
            if edge['is_primary']:
                writer.writerow([edge['source_type'], edge['source_property_name'], edge['destination_type']])
    print(f'primary edges written to {fn}')


if __name__ == '__main__':
    cli()
