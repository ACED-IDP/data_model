import csv
import importlib
import json
import threading
from collections import defaultdict
from dataclasses import dataclass
from importlib import import_module
from typing import TextIO, Optional, List

import click
import pathlib

import inflection
from fhir.resources import FHIRAbstractModel
from fhir.resources.bundle import Bundle
from fhir.resources.core.utils.common import normalize_fhir_type_class, get_fhir_type_name
from fhir.resources.fhirprimitiveextension import FHIRPrimitiveExtension
from yaml import SafeLoader
from fhir.resources.core.utils import yaml
from fhir.resources.reference import Reference
from flatten_json import flatten as flatten_dict
from dataclass_csv import DataclassWriter

import orjson

# thread local instance for our Reference renderer
LINKS = threading.local()
CLASSES = threading.local()


@dataclass
class EdgeInfo:
    dst_name: str
    """Destination Vertex"""
    src_name: str
    """Source Vertex"""
    src_property: str
    """Source property"""
    src_parent: Optional[str] = None
    """Parent of src_property"""
    src_docstring: Optional[str] = None
    """Docstring for source class"""
    src_property_docstring: Optional[str] = None
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


@click.group('bundle')
def bundle():
    pass


@bundle.command('etl')
@click.option('--input_path', required=True,
              default=None,
              show_default=True,
              help='Path to bundle json'
              )
@click.option('--output_path', required=True,
              default=None,
              show_default=True,
              help='Path to ndjson'
              )
@click.option('--flatten', default=True, show_default=True, is_flag=True,
              help='Flatten objects')
def bundle_etl(input_path, output_path, flatten):
    """Read a bundle of FHIR data, for each entry, render a flat object, xform its References to links."""
    # check params
    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    assert input_path.is_dir()
    assert output_path.is_dir()

    # where we will store discovered References
    global LINKS
    LINKS.links = []

    # open file pointers
    emitters = {}

    def emitter(name: str) -> TextIO:
        """Maintain a hash of open files."""
        if name not in emitters:
            emitters[name] = open(output_path / f"{name}.ndjson", "w")
        return emitters[name]

    def links(self: Reference, *args, **kwargs):
        """Render a `link`, assign to thread local, then call the original dict function."""
        global LINKS
        # note `self` is the embedded Reference
        parts = self.reference.split('/')
        dst_id = parts[-1]
        dst_name = inflection.underscore(parts[0])
        # fhir/resources/core/fhirabstractmodel.py passes the pydantic Field of the value as a param to dict()
        assert 'field' in kwargs, kwargs
        if 'backref' in kwargs['field'].field_info.extra:
            backref = kwargs['field'].field_info.extra['backref']
        else:
            backref = str(kwargs['field'].model_config.__module__).split('.')[-1]

        LINKS.links(links=links.append({"dst_id": dst_id, "dst_name": dst_name, "label": dst_name, "backref": backref}))
        return orig_dict(self, *args, **kwargs)

    # patch References' dict method with our method
    orig_dict = Reference.dict
    Reference.dict = links

    # render all bundles into vertex and edges
    for input_file in input_path.glob('*.json'):
        print(input_file)
        bundle_ = Bundle.parse_file(
            input_file, content_type="application/json", encoding="utf-8"
        )
        for entry in bundle_.entry:
            obj = orjson.loads(entry.resource.json())
            if flatten:
                obj = flatten_dict(obj, separator='_')
            fp = emitter(entry.resource.resource_type)
            vertex_and_edges = {
                'id': entry.resource.id,
                'name': entry.resource.resource_type,
                'relations': LINKS.links,
                'object': obj,
            }
            json.dump(vertex_and_edges, fp, separators=(',', ':'))
            fp.write('\n')
            # reset shared object
            LINKS.links = []
            break

    # close all emitters
    for fp in emitters.values():
        fp.close()

    # restore the original dict method
    Reference.dict = orig_dict


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
            schema = yaml.load(fp, SafeLoader)

            vertex = next(iter([k for k in schema.keys() if not k.startswith('$')]), None)

            schema = schema[vertex]
            assert 'title' in schema, schema
            schema['$id'] = f"{base_uri}/{schema['title']}"

            if 'properties' in schema:
                for p_ in schema['properties'].values():
                    if 'type' not in p_:
                        continue
                    if p_['type'][0].isupper():
                        p_['$ref'] = f"{base_uri}/{p_['type']}"
                        del p_['type']
                        continue

                    if p_['type'] == 'array' and 'items' in p_ and 'type' in p_['items']:
                        if p_['items']['type'][0].isupper():
                            p_['items']['$ref'] = f"{base_uri}/{p_['items']['type']}"
                            del p_['items']['type']

            schemas['$defs'][schema['title']] = schema
            schemas['anyOf'].append({'$ref': f"{base_uri}/{schema['title']}"})

    with open(output_path / pathlib.Path("coherent.json"), "w") as fp:
        json.dump(schemas, fp, indent=2)

    print(f"Uber schema written to {output_path / pathlib.Path('all.json')}")


@bundle.command('schema')
@click.option('--input_path', required=True,
              default=None,
              show_default=True,
              help='Path to bundle json'
              )
@click.option('--output_path', required=True,
              default=None,
              show_default=True,
              help='Path to schema files'
              )
@click.option('--config_path',
              default='gen3.config.yaml',
              show_default=True,
              help='Path to gen3 config file.')
def bundle_schema(input_path, output_path, config_path):
    """Extract a schema for all objects in the bundle."""
    # https://github.com/uc-cdis/datadictionary/blob/develop/gdcdictionary/schemas/README.md

    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    config_path = pathlib.Path(config_path)
    assert input_path.is_dir()
    assert output_path.is_dir()
    assert config_path.is_file()
    with open(config_path) as fp:
        _ = yaml.load(fp, SafeLoader)
        config_paths = _['paths']
        config_categories = _['categories']
        ignored_properties = _['ignored_properties']

    # our namespace
    base_uri = 'http://bmeg.io/schema/0.0.1'

    # where we will store discovered References
    global CLASSES
    CLASSES.classes = set()

    def classes(self: FHIRAbstractModel, *args, **kwargs):
        """Render a `link`, assign to thread local, then call the original dict function."""
        global CLASSES
        # note `self` is any FHIR object
        CLASSES.classes.add(self.__class__)
        return orig_dict(self, *args, **kwargs)

    # patch References' dict method with our method
    orig_dict = FHIRAbstractModel.dict
    FHIRAbstractModel.dict = classes

    # limit classes to what data actually exists, so lets
    # look at the data
    # render all bundles into vertex and edges
    for input_file in input_path.glob('*.json'):
        print(input_file)
        bundle_ = Bundle.parse_file(
            input_file, content_type="application/json", encoding="utf-8"
        )
        for entry in bundle_.entry:
            # trigger our reference collection
            entry.resource.dict()
        # break  # just check one file for quick testing

    # restore the original dict method
    FHIRAbstractModel.dict = orig_dict

    # find subclasses 3 levels deep
    found_in_data = set([klass.__name__ for klass in CLASSES.classes])
    for _ in range(3):
        embedded_classes = set()
        for klass in CLASSES.classes:
            for p in klass.element_properties():
                mod = importlib.import_module('fhir.resources')
                try:
                    embedded_class = mod.get_fhir_model_class(get_fhir_type_name(p.type_))
                    embedded_classes.add(embedded_class)
                except KeyError:
                    pass
        CLASSES.classes.update(embedded_classes)
        CLASSES.classes.add(FHIRPrimitiveExtension)

    #
    # render backref information
    #

    # collect edge info
    edges, edge_names = _extract_edge_info(CLASSES)

    incoming_edges = _filter_incoming_edges(edges)

    schemas = {}
    for klass in CLASSES.classes:

        schema = klass.schema()
        _add_reference_backrefs(incoming_edges, schema)

        schema['description'] = schema.get('description', '').replace('\n\n', '\n')

        with open(output_path / pathlib.Path(klass.__name__ + ".yaml"), "w") as fp:
            yaml.dump({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{base_uri}/{klass.__name__}",
                klass.__name__: schema
            }, fp)

        schemas[klass.__name__] = schema

    print('Schema yaml files written to', output_path)

    # write our EdgeConfig type
    edge_config_yaml = f"""
        type: object
        title: EdgeConfig
        "$id": {base_uri}/EdgeConfig
        description: Information about the edge between two nodes.
        properties:
          resource_type:
            description: The title of the resource
            type: string
          source:
            description: A pointer to the source resource, encapsulates type & id.
            "$ref": {base_uri}/Reference
          destination:
            description: A pointer to the destination resource, encapsulates type & id.
            "$ref": {base_uri}/Reference
        required:
        - resource_type
        - source
        - destination    
    """
    schema = yaml.yaml_loads(edge_config_yaml)
    with open(output_path / pathlib.Path("EdgeConfig" + ".yaml"), "w") as fp:
        yaml.dump({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{base_uri}/EdgeConfig",
            "EdgeConfig": schema
        }, fp)

    schemas[klass.__name__] = schema


    # extra_property_keys = {}
    # for klass in CLASSES.classes:
    #     # document the set of extra, non-standard json schema fields exist
    #     for p in klass.element_properties():
    #         for k in p.field_info.extra.keys():
    #             extra_property_keys[k] = {'type': type(p.field_info.extra[k]).__name__}
    # from pprint import pprint as pp
    # pp(('The schemas contain these "extra" keys', extra_property_keys))

    edge_counts = defaultdict(dict)
    for edge_info in edges:
        if edge_info.dst_name not in edge_counts[edge_info.src_name]:
            edge_counts[edge_info.src_name][edge_info.dst_name] = 0
        edge_counts[edge_info.src_name][edge_info.dst_name] += 1

    for edge_info in edges:
        if not (edge_info.src_name in edge_names and edge_info.dst_name in edge_names):
            continue
        title = f"{edge_info.src_name}_{edge_info.src_property}_{edge_info.dst_name}"
        is_primary = edge_counts[edge_info.src_name][edge_info.dst_name] == 1
        if not is_primary and edge_info.src_property == 'subject':
            is_primary = True
        # TODO - these are the unresolved multigraphs for coherent data set. Get from config file?
        if not is_primary and title in [
          'Signature_who_Patient',
          'Task_owner_Patient',
          'MedicationRequest_basedOn_MedicationRequest',
          'Observation_hasMember_Observation',
        ]:
            is_primary = True

        edge_config_template = f"""
        "$id": {base_uri}/{title}
        description: {edge_info.src_property_docstring}. (Generated Edge)
        # start vocabulary fields
        source_property_name: {edge_info.src_property}
        source_multiplicity: many
        destination_property_name: {edge_info.backref}
        destination_multiplicity: many
        label: {title}
        is_primary: {is_primary}
        source_type: {edge_info.src_name}
        destination_type: {edge_info.dst_name}
        # end vocabulary fields
        type: object
        title: {title}
        allOf:
        - "$ref": {base_uri}/EdgeConfig    
        """
        schema = yaml.yaml_loads(edge_config_template)
        with open(output_path / pathlib.Path(f"Edge_{title}" + ".yaml"), "w") as fp:
            yaml.dump({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{base_uri}/{title}",
                title: schema
            }, fp)

    print('edges schemas written')

    _bundle_schemas(output_path, base_uri)

    _gen3_schemas(config_categories, config_paths, found_in_data, ignored_properties, output_path, schemas)


def _filter_incoming_edges(edges):
    # order it by dst str
    incoming_edges = defaultdict(dict)
    for edge in edges:
        if edge.src_name not in incoming_edges[edge.dst_name]:
            incoming_edges[edge.dst_name][edge.src_name] = {'count': 0, 'backrefs': [], 'docstrings': {}}
        incoming_edges[edge.dst_name][edge.src_name]['count'] += 1
    # loop through edges again, determine backref
    for edge in edges:
        backref = inflection.underscore(edge.src_name)
        if incoming_edges[edge.dst_name][edge.src_name]['count'] > 1:
            backref = f"{edge.src_property}_{inflection.underscore(edge.src_name)}"
        incoming_edges[edge.dst_name][edge.src_name]['backrefs'].append(backref)
        incoming_edges[edge.dst_name][edge.src_name]['docstrings'][backref] = edge.src_property_docstring
        edge.backref = backref
    return incoming_edges


def _extract_edge_info(CLASSES):
    edges = []
    edge_names = []
    for klass in CLASSES.classes:
        if not hasattr(klass, '__fields__'):
            continue
        if hasattr(klass, 'is_primitive') and klass.is_primitive():
            continue
        for k, v in klass.__fields__.items():
            if 'enum_reference_types' in v.field_info.extra:
                for enum_reference_type in set(v.field_info.extra['enum_reference_types']):
                    edge_info = EdgeInfo(**{
                        'dst_name': enum_reference_type,
                        'src_name': klass.__name__,
                        'src_docstring': klass.__doc__.replace('\n', ' ').replace('  ', ' ').replace('  ', ' ').strip(),
                        'src_property': k,
                        'src_property_docstring': v.field_info.title,
                        'src_parent': v.field_info.extra.get('parent_name', None),
                    })
                    edges.append(edge_info)
                    edge_names.append(klass.__name__)
    return edges, edge_names


def _add_reference_backrefs(incoming_edges, schema):
    # set the backref
    schema['$id'] = schema['title']
    for k, p in schema['properties'].items():
        if 'enum_reference_types' not in p:
            continue
        src_name = schema['title']
        reference_backrefs = {}
        for dst_name in p['enum_reference_types']:
            check_property_name = len(incoming_edges[dst_name][src_name]['backrefs']) > 1
            for backref in incoming_edges[dst_name][src_name]['backrefs']:
                if check_property_name and backref.startswith(k):
                    reference_backrefs[dst_name] = backref
                elif not check_property_name:
                    reference_backrefs[dst_name] = backref
        assert len(reference_backrefs) > 0, (k, src_name)
        del p['enum_reference_types']
        p['reference_backrefs'] = reference_backrefs


def _gen3_schemas(config_categories, config_paths, found_in_data, ignored_properties, output_path, schemas):
    #
    # xform for gen3
    #
    # delete vertices we don't want
    vertices_to_delete = set(schemas.keys()) - found_in_data
    for k in vertices_to_delete:
        del schemas[k]
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

        # add links
        src_name = schema['title']
        links = []
        for src_property, p in schema['properties'].items():
            if 'reference_backrefs' not in p:
                continue
            for dst_name, backref in p['reference_backrefs'].items():
                # schema can define multiple targets, limit target to those actually in this dataset
                if dst_name not in schemas:
                    continue

                # schema can define multiple targets, limit target to those actually in this dataset
                if dst_name not in schemas:
                    continue

                if src_name in config_paths and dst_name in config_paths[src_name]:
                    if src_property not in config_paths[src_name][dst_name]:
                        continue

                links.append({
                    'backref': backref,
                    'label': f"{src_name}_{src_property}_{dst_name}",
                    'multiplicity': 'many_to_many',
                    'name': f"{src_name}_{dst_name}",
                    'required': False,
                    'target_type': dst_name
                })

                linked_vertices.add(dst_name)
                linked_vertices.add(src_name)

        # flag links to same vertex
        # vertices with more than one edge
        edge_counts = defaultdict(set)
        for link in links:
            edge_counts[link['target_type']].add(link['label'])
        edges_to_filter = [(k, v) for k, v in edge_counts.items() if len(v) > 1]
        for (dst_name, labels) in edges_to_filter:
            subject_edge = next(iter([edge_label for edge_label in labels if 'subject' in edge_label]), None)
            if subject_edge:
                print('path', k, subject_edge.split('_')[1], labels)
                continue
            else:
                print('path?', k, labels)

        # if len(edges_to_filter) > 0:
        #     subject = [k for k, v in edges_to_filter.items() if x]
        #     for k, v in edges_to_filter.items():
        #         if
        #     print(schema['title'], edges_to_filter)

        # assign links to vertex
        schema["links"] = links

        # simplify. In this case, by deleting complex types
        properties_to_delete = []
        for name_, property_ in schema['properties'].items():
            if 'type' not in property_:
                properties_to_delete.append(name_)
                continue
            if property_['type'] in ['array']:
                properties_to_delete.append(name_)
                continue
            if property_['type'][0].isupper() and property_['type'] not in ['Coding', 'CodeableConcept', 'Identifier']:
                properties_to_delete.append(name_)
                continue
            if name_ in ignored_properties:
                properties_to_delete.append(name_)
                continue
        for name_ in properties_to_delete:
            del schema['properties'][name_]
            # print(f"Deleted {name_} from {k}")

        # simplified property types, cast to string
        for name_, property_ in schema['properties'].items():
            if property_['type'] in ['Coding', 'CodeableConcept', 'Identifier']:
                property_['type'] = 'string'

        # simplified property types, cast to string
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

        # add gen3 file scaffolding
        if schema['title'] == 'DocumentReference':
            snippet = """
              $ref: "_definitions.yaml#/data_file_properties"

              data_category:
                term:
                  $ref: "_terms.yaml#/data_category"
                type: string
              data_type:
                term:
                  $ref: "_terms.yaml#/data_type"
                type: string
              data_format:
                term:
                  $ref: "_terms.yaml#/data_format"
                type: string
            """
            snippet = yaml.load(snippet, Loader=SafeLoader)
            for snippet_key, snippet_value in snippet.items():
                schema['properties'][snippet_key] = snippet_value
    # discard vertices that are not actually linked
    vertices_to_delete = set(schemas.keys()) - linked_vertices
    for k in vertices_to_delete:
        del schemas[k]
    # add gen3 scaffolding
    with open(output_path / pathlib.Path("_definitions.yaml")) as fp:
        schemas["_definitions.yaml"] = yaml.load(fp, SafeLoader)
    with open(output_path / pathlib.Path("_terms.yaml")) as fp:
        schemas["_terms.yaml"] = yaml.load(fp, SafeLoader)
    # save gen3 schema
    with open(output_path / pathlib.Path("coherent.gen3.json"), "w") as fp:
        json.dump(schemas, fp)
    print('Gen3 schemas written to', output_path / pathlib.Path("coherent.gen3.json"))
    #
    # done with gen3 xform
    #


@bundle.command('cytoscape')
@click.option('--input_path', required=True,
              default=None,
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
    assert input_path.is_dir()
    assert output_path.is_dir()

    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    assert input_path.is_dir()
    assert output_path.is_dir()
    edges = []
    for input_file in input_path.glob('*.yaml'):
        print(input_file)
        with open(input_file) as fp:
            schema = yaml.load(fp, Loader=SafeLoader)
            vertex = next(iter([v for k, v in schema.items() if k != '$schema']), None)

            assert 'title' in vertex, vertex
            src_name = vertex['title']
            for src_property, p in vertex['properties'].items():
                if 'reference_backrefs' not in p:
                    continue
                for dst_name, backref in p['reference_backrefs'].items():
                    edges.append(
                        EdgeInfo(src_name=src_name, src_property=src_property, dst_name=dst_name, backref=backref))

    fn = output_path / pathlib.Path('edges.sif.combined.tsv')
    with open(fn, 'w', newline='') as tsv_file:
        writer = csv.writer(tsv_file, delimiter='\t', lineterminator='\n')
        writer.writerow(['sourceName', '(edgeType)', 'targetName'])
        for edge in edges:
            writer.writerow([edge.dst_name, edge.backref, edge.src_name])

        for edge in edges:
            writer.writerow([edge.src_name, edge.src_property, edge.dst_name])

    print(f'edges written to {fn}')


@bundle.command('gen3')
@click.option('--input_path', required=True,
              default=None,
              show_default=True,
              help='Path to bundle json'
              )
@click.option('--output_path', required=True,
              default=None,
              show_default=True,
              help='Path to gen3 output files'
              )
@click.option('--schema_path',
              default='coherent.json',
              show_default=True,
              help='Path to gen3 config file.')
def bundle_gen3(input_path, output_path, schema_path):
    """Extract a schema for all objects in the bundle."""
    # https://github.com/uc-cdis/datadictionary/blob/develop/gdcdictionary/schemas/README.md

    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    schema_path = pathlib.Path(schema_path)
    assert input_path.is_dir()
    assert output_path.is_dir()
    assert schema_path.is_file()
    with open(schema_path) as fp:
        schema = json.load(fp)

    # render all bundles into vertex and edges
    for input_file in input_path.glob('*.json'):
        print(input_file)
        bundle_ = Bundle.parse_file(
            input_file, content_type="application/json", encoding="utf-8"
        )
        for entry in bundle_.entry:
            r_ = entry.resource.dict()
            print(r_)
        # break  # just check one file for quick testing


if __name__ == '__main__':
    bundle()
