import csv
import importlib
import json
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
from fhir.resources.core.utils import yaml
from fhir.resources.reference import Reference
from flatten_json import flatten as flatten_dict
from dataclass_csv import DataclassWriter

import orjson

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


@click.group('bundle')
def bundle():
    pass


def _simple_render(value, name_, **kwargs) -> str:

    def _to_list(x):
        """Ensure item is list."""
        if isinstance(x, list):
            return x
        return[x]

    def _name_values(name, values):
        """Adds index to name if list > 1."""
        # return list of tuple name, value

        if not values:
            return [(name, values)]

        _nv = []
        for i_, v_ in enumerate(values):
            if i_ > 0:
                _nv.append((f"{name}_{i_}", v_))
            else:
                _nv.append((name, v_))
        return _nv

    def _noop(v_, name, **kwargs):
        return _name_values(name, [v_])

    def _to_str(v_, name, **kwargs):
        return _name_values(name, [str(v_)])

    def _to_iso(l_, name, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [v_.isoformat() for v_ in l_])

    def _codeable_concept(l_, name, **kwargs):
        l_ = _to_list(l_)
        _nv = []
        for i_, v_ in enumerate(l_):
            codings = _coding(v_.coding, 'coding')
            text_ = v_.text
            array_name = name
            if i_ > 0:
                array_name = name + f"_{i_}"
            _nv.append((f"{array_name}", text_))

            for coding in codings:
                if 'coding_display' in coding[0] and text_:
                    continue
                if 'coding_display' in coding[0]:
                    _nv.append((array_name, coding[1]))
                    # first coding wins display
                    text_ = coding[1]
                    continue
                _nv.append((f"{array_name}_{coding[0]}", coding[1]))
        return _nv

    def _contact_point(l_, name, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [f"{v_.system}:{v_.use}:{v_.value}" for v_ in l_])

    def _identifier(l_, name, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [f"{v_.system}#{v_.value}" for v_ in l_])

    def _patient_communication(l_, name, **kwargs):
        l_ = _to_list(l_)
        _nv = []
        array_name = name
        for i_, v_ in enumerate(l_):
            codeable_concepts = _codeable_concept(v_.language, 'language')
            if i_ > 0:
                array_name = name + f"_{i_}"
            # first codeable concept is default
            _nv.append((array_name, codeable_concepts[0][1]))
            # skip the rest
            # for codeable_concept in codeable_concepts:
            #     _nv.append((f"{array_name}_{codeable_concept[0]}", codeable_concept[1]))
        return _nv

    def _human_name(l_, name, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [f"{v_.family} {' '.join(v_.given)}" for v_ in l_])

    def _address(l_, name, **kwargs):
        l_ = _to_list(l_)
        for v_ in l_:
            # first one wins
            a_ = []
            for _ in v_.line or []:
                a_.append(_)
            a_.extend([v_.city, v_.postalCode, v_.country])
            return name, ' '.join([_ for _ in a_ if _])

    def _reference(l_, name, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [v_.reference for v_ in l_])

    def _document_reference_content(l_, name, **kwargs):
        l_ = _to_list(l_)
        urls = []
        for v_ in l_:
            if v_.attachment.data:
                urls.append(f'data:,{str(v_.attachment.data, encoding="ascii", errors="ignore")}')
            else:
                urls.append(v_.attachment.url)
        return _name_values(name + "_url", urls)

    def _quantity(l_, name, **kwargs):
        l_ = _to_list(l_)
        _nv = []
        for i_, v_ in enumerate(l_):
            array_name = name
            if i_ > 0:
                array_name = name + f"_{i_}"
            _nv.append((f"{array_name}", f"{v_.value} {v_.unit}"))
            if v_.value is None:
                print("?")
            _nv.append((f"{array_name}_value", float(str(v_.value))))  # decimal.Decimal
            _nv.append((f"{array_name}_unit", f"{v_.system}#{v_.unit}"))
        return _nv

    def _coding(l_, name, **kwargs):
        l_ = _to_list(l_)

        # quick fix for coherent data, should really be in coherent_refactor_bundle
        for v_ in l_:
            if v_.display == 'survey':
                v_.display = 'Survey'

        _nv = _name_values(f"{name}_display", [f"{v_.display}" for v_ in l_])
        _nv.extend(_name_values(name, [f"{v_.system}#{v_.code}" for v_ in l_]))
        return _nv

    def _multi(v_, prefix):
        """Return single tuple"""
        for attr in [a_ for a_ in vars(v_) if a_.startswith(prefix) and not a_.endswith('_ext')]:
            p_val = getattr(v_, attr)
            if p_val:
                list_of_name_vals = _simple_render(p_val, name_=attr)
                # ignore the name from simple render
                return [nv_[1] for nv_ in list_of_name_vals]

    # # this version flattens, does not create property names
    # def _observation_component(l_, name, **kwargs):
    #     l_ = _to_list(l_)
    #     _nv = []
    #     for i_, v_ in enumerate(l_):
    #         codings = _codeable_concept(v_.code, 'code')
    #         value_parts = _multi(v_, prefix='value')
    #         array_name = name
    #         if i_ > 0:
    #             array_name = name + f"_{i_}"
    #         for coding in codings:
    #             _nv.append((f"{array_name}_{coding[0]}", coding[1]))
    #         for value_part in value_parts:
    #             _nv.append((f"{array_name}_{value_part[0]}", value_part[1]))
    #     return _nv

    def _observation_component(l_, name, **kwargs):
        """Create property names from component coding displays"""
        l_ = _to_list(l_)
        _nv = []
        for i_, v_ in enumerate(l_):
            value_parts = _multi(v_, prefix='value')
            array_name = v_.code.coding[0].display.replace('-', '_').replace(' ', '_').lower()
            for value_part in value_parts:
                # remove valueXXX
                edited_name = '_'.join(value_part[0].split('_')[1:])
                edited_name = f"{array_name}_{edited_name}"
                if edited_name.endswith('_'):
                    edited_name = edited_name[:-1]
                _nv.append((edited_name, value_part[1]))
        return _nv

    def _task_input(l_, name, **kwargs):
        l_ = _to_list(l_)
        _nv = []
        for i_, v_ in enumerate(l_):
            codings = _codeable_concept(v_.type, 'type')
            value_parts = _multi(v_, prefix='value')
            array_name = name
            if i_ > 0:
                array_name = name + f"_{i_}"
            for value_part in value_parts:
                _nv.append((f"{array_name}_{value_part[0]}", value_part[1]))
            for coding in codings:
                _nv.append((f"{array_name}_{coding[0]}", coding[1]))
        return _nv

    def _task_output(l_, name, **kwargs):
        l_ = _to_list(l_)
        _nv = []
        for i_, v_ in enumerate(l_):
            codings = _codeable_concept(v_.type, 'type')
            value_parts = _multi(v_, prefix='value')
            array_name = name
            if i_ > 0:
                array_name = name + f"_{i_}"
            for value_part in value_parts:
                _nv.append((f"{array_name}_{value_part[0]}", value_part[1]))
            for coding in codings:
                _nv.append((f"{array_name}_{coding[0]}", coding[1]))
        return _nv

    def _sampled_data(l_, name, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [v_.data for v_ in l_])

    # # this version flattens, does not create property names
    # def _extension(l_, name, depth=0, **kwargs):
    #     if depth == 2:
    #         return []
    #     l_ = _to_list(l_)
    #     _nv = []
    #     for i_, v_ in enumerate(l_):
    #         array_name = name
    #         if i_ > 0 and depth == 0:
    #             array_name = name + f"_{i_}"
    #         if depth == 0:
    #             _nv.append((f"{array_name}_url", v_.url))
    #         value_parts = _multi(v_, prefix='value')
    #         if value_parts:
    #             for value_part in value_parts:
    #                 if depth == 0:
    #                     _nv.append((f"{array_name}_{value_part[0]}", value_part[1]))
    #                 else:
    #                     _nv.append((f"{value_part[0]}", value_part[1]))
    #         else:
    #             extensions = _extension(v_.extension, name=name, depth=depth+1)
    #             for extension in extensions:
    #                 _nv.append((f"{array_name}_{extension[0]}", extension[1]))
    #     return _nv

    def _extension(l_, name, depth=0, **kwargs):
        """Creates property names from """
        if depth == 2:
            return []
        l_ = _to_list(l_)
        _nv = []
        for i_, v_ in enumerate(l_):
            array_name = v_.url.split('/')[-1].replace('-', '_')
            value_parts = _multi(v_, prefix='value')
            if value_parts:
                for value_part in value_parts:
                    # remove valueXXX
                    # edited_name = value_part[0]
                    edited_name = value_part[0].replace('valueCoding_display', '').replace('valueString', '').replace('valueCoding', 'coding').replace('valueCode', 'code')
                    if len(edited_name) > 0:
                        edited_name = f"_{edited_name}"
                    edited_name = edited_name.replace('__', '_')

                    if depth == 0:
                        _nv.append((f"{array_name}{edited_name}", value_part[1]))
                    else:
                        # don't add extra suffixes on recursion
                        _nv.append((f"{edited_name}", value_part[1]))
            else:
                extensions = _extension(v_.extension, name=array_name, depth=depth+1)
                for extension in extensions:
                    # remove valueXXX
                    edited_name = extension[0].replace('valueCoding_display', '').replace('valueString', '').replace('valueCoding', 'coding').replace('valueCode', 'code')
                    if len(edited_name) > 0:
                        edited_name = f"_{edited_name}"
                    edited_name = edited_name.replace('__', '_')
                    _nv.append((f"{array_name}{edited_name}", extension[1]))
        return _nv

    def _decimal(l_, name, depth=0, **kwargs):
        l_ = _to_list(l_)
        return _name_values(name, [float(str(v_)) for v_ in l_])

    mapping = {
        'str': _noop,
        'NoneType': _noop,
        'bool': _noop,
        'date': _to_iso,
        'datetime': _to_iso,
        'CodeableConcept': _codeable_concept,
        'ContactPoint': _contact_point,
        'Identifier': _identifier,
        'PatientCommunication': _patient_communication,
        'HumanName': _human_name,
        'Address': _address,
        'Reference': _reference,
        'DocumentReferenceContent': _document_reference_content,
        'Quantity': _quantity,
        'ObservationComponent': _observation_component,
        'TaskInput': _task_input,
        'TaskOutput': _task_output,
        'SampledData': _sampled_data,
        'Extension': _extension,
        'Decimal': _decimal,
        'Coding': _coding,
        'int': _noop,
    }

    _type = type(value)
    if isinstance(value, list):
        _type = type(value[0])
    if _type.__name__ not in mapping:
        print(f"H2 {_type}")
        return [(name_, str(value))]

    mapped_values = mapping[_type.__name__](value, name=name_)

    named_mapped_values = []
    if mapped_values and isinstance(mapped_values, list):
        for index, mapped_value in enumerate(mapped_values):
            if index > 0:
                name_ = f"{name_}_{index}"
            named_mapped_values.append((name_, mapped_value))
    else:
        named_mapped_values.append((name_, mapped_values))
    return named_mapped_values


def _simplify(resource: FHIRAbstractModel, schemas: dict) -> dict:
    """Simplify this FHIR resource, adjust for Gen3"""
    # resource.schema()
    if inflection.underscore(resource.resource_type) not in schemas:
        return {}
    schema = schemas[inflection.underscore(resource.resource_type)]
    properties = [p_ for p_ in resource.element_properties()]
    obj = {}
    for p_ in properties:
        if p_.name not in schema['properties']:
            continue
        p_val = getattr(resource, p_.name)
        if not p_val or p_val == {}:
            continue
        for item in _simple_render(p_val, name_=p_.name):
            if len(item[1]) != 2:
                print(f"Error: no mapping for {p_.name} {p_val}")
            name, value = item[1]
            if name not in schema['properties']:
                print(f"Skipping {resource.resource_type}.{name} - not in schema.")
                continue
            # skip nulls
            if value:
                obj[name] = value

    # gen3 boiler plate
    if resource.resource_type == 'DocumentReference':
        attachment = resource.content[0].attachment
        if not attachment.url:
            print(f"WARNING: attachment missing url. DocumentReference.{resource.id}")
        else:
            obj['data_type'] = attachment.url.split('.')[-1]
            if obj['data_type'] in ['csv']:
                obj['data_format'] = 'variants'
            if obj['data_type'] in ['dcm']:
                obj['data_format'] = 'imaging'
            if obj['data_type'] in ['txt']:
                obj['data_format'] = 'note'
            obj['file_name'] = attachment.url
            md5sum = next(iter([e.valueString for e in attachment.extension if
                                e.url == "http://aced-idp.org/fhir/StructureDefinition/md5"]), None)
            obj['md5sum'] = md5sum
            obj['file_size'] = 0
            if attachment.size:
                obj['file_size'] = int(attachment.size)

        obj['object_id'] = resource.id

    return obj


@bundle.command('transform')
@click.option('--input_path', required=True,
              default=None,
              show_default=True,
              help='Path containing bundles (*.json) or resources (*.ndjson)'
              )
@click.option('--output_path', required=True,
              default=None,
              show_default=True,
              help='Path where ndjson resources will be stored'
              )
@click.option('--schema_path', required=True,
              default='generated-json-schema/coherent.gen3.json',
              show_default=True,
              help='Path to gen3 schema json'
              )
@click.option('--duplicate_ids_for', default=None, show_default=True,
              help='Intentionally duplicate all graph ids. Provide study name.')
def bundle_transform(input_path, output_path, schema_path, duplicate_ids_for):
    """Read a bundle of FHIR data, for each entry, render a flat Gen3 friendly object."""
    # check params
    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    schema_path = pathlib.Path(schema_path)
    assert input_path.is_dir()
    assert output_path.is_dir()
    assert schema_path.is_file()

    # where we will store discovered References
    global LINKS
    LINKS.links = []

    # open file pointers
    emitters = {}

    _study_uuid = None
    if duplicate_ids_for:
        _study_uuid = uuid.uuid5(ACED_NAMESPACE, duplicate_ids_for)

    def emitter(name: str) -> TextIO:
        """Maintain a hash of open files."""
        if name not in emitters:
            emitters[name] = open(output_path / f"{name}.ndjson", "w")
            # print("Writing to", output_path / f"{name}.ndjson")
        return emitters[name]

    #
    # Gather the resource's References by patching the `dict` method
    # when the callback happens, populate a thread local with the reference
    # This enables us to gather links from all the embedded occurrences without having
    # to maintain either jsonpath or weird recursions.
    #
    def links(self: Reference, *args, **kwargs):
        """Render a `link`, assign to thread local, then call the original dict function."""
        global LINKS
        # note `self` is the embedded Reference
        parts = self.reference.split('/')
        dst_id = parts[-1]
        dst_name = parts[0]
        # fhir/resources/core/fhirabstractmodel.py passes the pydantic Field of the value as a param to dict()

        LINKS.links.append({"dst_id": dst_id, "dst_name": dst_name})
        return orig_dict(self, *args, **kwargs)

    # patch References' dict method with our method
    orig_dict = Reference.dict
    Reference.dict = links

    def _emit_vertex(resource):
        # trigger links
        resource.dict()
        # if resource.id == "90afbb8d-c8e9-5008-8f27-c2a59602cf14":
        #     print("?")

        schema_links = schemas[inflection.underscore(resource.resource_type)]['links']
        link_instances = []
        # de-dup
        already_added = set()
        if len(schema_links) > 0:
            for link_reference in LINKS.links:
                for schema_link in schema_links:
                    if inflection.underscore(link_reference['dst_name']) == schema_link['target_type']:
                        if link_reference['dst_id'] in already_added:
                            continue
                        link_instances.append(link_reference)
                        already_added.add(link_reference["dst_id"])

        obj = _simplify(resource, schemas)

        fp = emitter(resource.resource_type)

        # refactor ids, make them relative to study name
        if _study_uuid:
            original = resource.id
            id_ = str(uuid.uuid5(_study_uuid, resource.id))
            resource.id = id_
            obj['id'] = id_
            for lnk in link_instances:
                lnk['dst_id'] = str(uuid.uuid5(_study_uuid, lnk['dst_id']))
            if resource.resource_type == "DocumentReference":
                # also adjust the indexd identifier
                obj['object_id'] = id_

        vertex_and_edges = {
            'id': resource.id,
            'name': resource.resource_type,
            'relations': link_instances,
            'object': obj,
        }
        json.dump(vertex_and_edges, fp, separators=(',', ':'))
        fp.write('\n')
        # reset shared object
        LINKS.links = []

    with open(schema_path) as fp_:
        schemas = json.load(fp_)

    # render all bundles into vertex and edges
    for input_file in input_path.glob('*.json'):
        print(input_file)
        bundle_ = Bundle.parse_file(
            input_file, content_type="application/json", encoding="utf-8"
        )
        for entry in bundle_.entry:
            resource_ = entry.resource
            if inflection.underscore(resource_.resource_type) not in schemas:
                # ignore this vertex
                continue
            _emit_vertex(resource_)

        # break  # after one file for testing

    mod = importlib.import_module('fhir.resources')

    for input_file in input_path.glob('*.ndjson'):
        print(input_file)
        logged_already = False
        with open(input_file) as fp_:
            for line in fp_.readlines():
                obj_ = orjson.loads(line)
                klass = mod.get_fhir_model_class(obj_['resourceType'])
                try:
                    resource_ = klass.parse_obj(obj_)
                except ValidationError as e:
                    if not logged_already:
                        print(f"ERROR: {obj_['id']} {e}")
                        logged_already = True
                    continue
                _emit_vertex(resource_)

    # close all emitters
    for fp_ in emitters.values():
        fp_.close()

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

    with open(output_path / pathlib.Path("aced-bmeg.json"), "w") as fp:
        json.dump(schemas, fp, indent=2)

    print(f"Uber schema written to {output_path / pathlib.Path('aced-bmeg.json')}")


@bundle.command('schema')
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
    # print(classes)

    # find subclasses 3 levels deep
    for _ in range(2):
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
        _add_reference_backrefs(incoming_edges, schema)
        schema['description'] = schema.get('description', '').replace('\n\n', '\n')
        with open(output_path / pathlib.Path(klass.__name__ + ".yaml"), "w") as fp:
            yaml.dump({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{base_uri}/{klass.__name__}",
                klass.__name__: schema
            }, fp)
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
        "$id": {base_uri}/{title}
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
        - "$ref": {base_uri}/EdgeConfig    
        """
        schema = yaml.yaml_loads(edge_config_template)
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


@bundle.command(name='schema-publish')
@click.argument('dictionary_path', default='generated-json-schema/aced.json')
@click.option('--bucket', default="s3://aced-public", help="Bucket target")
def schema_publish(dictionary_path, bucket):
    """Copy dictionary to s3 (note:aws cli dependency)"""

    dictionary_path = pathlib.Path(dictionary_path)
    assert dictionary_path.is_file(), f"{dictionary_path} should be a path"
    click.echo(f"Writing schema into {bucket}")
    import subprocess
    cmd = f"aws s3 cp {dictionary_path} {bucket}".split(' ')
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
        if klass.__name__ not in desired_vertices:
            continue
        if not hasattr(klass, '__fields__'):
            continue
        if hasattr(klass, 'is_primitive') and klass.is_primitive():
            continue
        for k, v in klass.__fields__.items():
            if 'enum_reference_types' in v.field_info.extra:
                for enum_reference_type in set(v.field_info.extra['enum_reference_types']):
                    if enum_reference_type not in desired_vertices:
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


def _add_reference_backrefs(incoming_edges, schema):
    # set the backref
    schema['$id'] = schema['title']
    for k, p in schema['properties'].items():
        if 'enum_reference_types' not in p:
            continue
        src_name = schema['title']
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
        del p['enum_reference_types']
        if len(reference_backrefs) > 0:
            p['reference_backrefs'] = reference_backrefs


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
        schema['id'] = inflection.underscore(schema['$id'])
        del schema['$id']

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
            if 'type' not in property_:
                properties_to_delete.append(name_)
                continue
            elif name_ in ignored_properties:
                properties_to_delete.append(name_)
                continue
            elif property_['type'] in ['array']:
                properties_to_delete.append(name_)
                if 'description' in property_:
                    property_['description'] += " (array)"

                if property_['items']['type'] in ['CodeableConcept']:
                    properties_to_add.append((f"{name_}_coding", {'type': 'string', 'title': "Coded representation."}))

                if property_['items']['type'] in ['Quantity']:
                    properties_to_add.append((f"{name_}_unit", {'type': 'string', 'title': "Unit representation."}))
                    properties_to_add.append((f"{name_}_value", {'type': 'number', 'title': "Numerical value (with implicit precision)"}))

                property_['type'] = 'string'

                is_expandable = property_['items']['type'] in ['Identifier']

                del property_['items']
                properties_to_add.append((name_, property_))
                if is_expandable:
                    for i in range(1, IDENTIFIER_LIST_SIZE):
                        properties_to_add.append(
                            (f"{name_}_{i}",
                             {'type': 'string', 'title': property_.get('title', 'An identifier.')}))
                continue
            elif 'one_of_many' in property_ and property_['type'] in schemas:
                if property_['type'] in ['CodeableConcept']:
                    properties_to_add.append((f"{name_}_coding", {'type': 'string', 'title': "Coded representation."}))
                if property_['type'] in ['Quantity']:
                    properties_to_add.append((f"{name_}_unit", {'type': 'string', 'title': "Unit representation."}))
                    properties_to_add.append(
                        (f"{name_}_value", {'type': 'number', 'title': "Numerical value (with implicit precision)"}))
                stringified_types.append(property_['type'])
                property_['type'] = 'string'
                needs_to_string.add(property_['type'])

            elif property_['type'] in ['CodeableConcept']:
                properties_to_add.append((f"{name_}_coding", {'type': 'string', 'title': "Coded representation."}))

            elif property_['type'] in ['Quantity']:
                properties_to_add.append((f"{name_}_unit", {'type': 'string', 'title': "Unit representation."}))
                properties_to_add.append(
                    (f"{name_}_value", {'type': 'number', 'title': "Numerical value (with implicit precision)"}))

            # TODO - delete me?
            elif property_['type'][0].isupper() and property_['type'] not in stringified_types:
                properties_to_delete.append(name_)
                continue

        for name_ in properties_to_delete:
            del schema['properties'][name_]

        for name_, property_ in properties_to_add:
            schema['properties'][name_] = property_

        # simplified property types, cast to string
        for name_, property_ in schema['properties'].items():
            if property_['type'] in stringified_types:
                property_['description'] += f" {property_['type']}"
                property_['type'] = 'string'

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

        # sort properties for readability
        _ = sorted(schema['properties'].keys())
        schema['properties'] = {k: schema['properties'][k] for k in _}

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


@bundle.command('cytoscape')
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
    bundle()
