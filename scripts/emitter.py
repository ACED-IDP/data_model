import abc
import collections
import importlib
import io
import json
import logging
import os
import pathlib
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from itertools import islice
from pathlib import Path
from pprint import pprint
from typing import Dict, Iterator, List, Optional, OrderedDict, Any, ClassVar, IO

import click
import fhirclient
import inflection
import psycopg2
import yaml
from dictionaryutils import DataDictionary, dictionary, dump_schemas_from_dir
from fhirclient.models.bundle import Bundle
from fhirclient.models.fhirreference import FHIRReference
from flatten_json import flatten
from pelican.dictionary import DataDictionaryTraversal
from pydantic import BaseModel, PrivateAttr

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

LOGGED_ALREADY = set({})

ACED_CODEABLE_CONCEPT = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced.ipd/CodeableConcept')


def chunk(arr_range, arr_size):
    """Iterate in chunks."""
    arr_range = iter(arr_range)
    return iter(lambda: tuple(islice(arr_range, arr_size)), ())


def _init_dictionary(root_dir_=None, dictionary_url=None):
    """Initialize gen3 data dictionary from either directory or url"""
    d = DataDictionary(root_dir=root_dir_, url=dictionary_url)
    dictionary.init(d)
    # the gdcdatamodel expects dictionary initiated on load, so this can't be
    # imported on module level
    from gdcdatamodel import models as md

    return d, md


def first_occurrence(message: str) -> bool:
    """Return True if we haven't logged message already."""
    if message in LOGGED_ALREADY:
        return False
    LOGGED_ALREADY.add(message)
    return True


class FHIRTypeAlias(BaseModel):
    """Aliases for FHIR primitives, helps normalization."""

    code: str
    """Code used in profiles."""
    json_type: str
    """Type used in json schema."""
    url: str
    """Actual url used to retrieve it."""


class AttributeEnum(BaseModel):
    """Information about an enumeration."""

    url: str
    """Value/Code set url."""

    restricted_to: List[str]
    """Enumerated values."""

    binding_strength: str
    """FHIR binding strength, how should this enumeration be validated?"""

    class_name: str
    """FHIR class name."""


FHIR_TYPES: Dict[str, FHIRTypeAlias] = {
    'http://hl7.org/fhirpath/System.String': FHIRTypeAlias(json_type='string',
                                                           code='http://hl7.org/fhirpath/System.String',
                                                           url='http://build.fhir.org/string.profile.json'),
    'http://hl7.org/fhirpath/System.Integer': FHIRTypeAlias(json_type='integer',
                                                            code='http://hl7.org/fhirpath/System.Integer',
                                                            url='http://build.fhir.org/integer.profile.json'),
    'http://hl7.org/fhirpath/System.Decimal': FHIRTypeAlias(json_type='decimal',
                                                            code='http://hl7.org/fhirpath/System.Decimal',
                                                            url='http://build.fhir.org/decimal.profile.json'),
    'http://hl7.org/fhirpath/System.Boolean': FHIRTypeAlias(json_type='boolean',
                                                            code='http://hl7.org/fhirpath/System.Boolean',
                                                            url='http://build.fhir.org/boolean.profile.json'),
}


def recursive_default_dict():
    """Recursive default dict."""
    return defaultdict(recursive_default_dict)


def _normalize_property_name(name):
    """Replace special characters."""
    return name.replace('.', '_').replace(':', '_').replace('-', '_')


class Element(BaseModel):
    """Entity root."""

    id: str


class SubmitterIdAlias(BaseModel):
    """Property constant."""
    identifier_system: str


class Link(Element):
    """Configuration of Edge between entities."""

    targetProfile: List[str]
    """Constrain edge target."""
    required: Optional[bool] = True
    """Warn if missing."""
    ignore: Optional[bool] = False
    """Don't generate anything."""


class LinkInstance(BaseModel):
    """Actual Edge between entities for gen3."""
    dst_id: str
    dst_name: str
    label: str


class Entity(Element):
    """A FHIR resource, or embedded profile."""

    category: str
    """Corresponds to Gen3's dictionary category, embedded resources are assigned 'sub-profile'."""
    links: Optional[Dict[str, Link]] = {}
    """Narrow the scope of links."""
    source: Optional[str]
    """Explicit url for the profile."""
    submitter_id: Optional[SubmitterIdAlias] = None
    """Alias for submitter_id."""


class Model(BaseModel):
    """Delegates to a collection of Entity."""

    entities: Optional[OrderedDict[str, Entity]] = collections.OrderedDict()
    dependency_order: Optional[List[str]] = []
    ignored_properties: Optional[List[str]] = []

    @staticmethod
    def parse_file(path: str, **kwargs) -> Any:
        """Use entity_name, the map key  as id.
        """
        with open(path, "rb") as fp:
            config_ = yaml.safe_load(fp)

        needs_adding = []
        # use name as id
        for entity_name, entity in config_['entities'].items():
            if 'id' not in entity:
                entity['id'] = entity_name
            if 'links' in entity:
                assert isinstance(
                    entity['links'],
                    dict
                ), f"Error parsing file {path} unexpected link type for {entity_name} {entity['links']}"
                for link_name, link in entity['links'].items():
                    if 'id' not in link:
                        link['id'] = link_name
            if entity['id'] not in config_['entities']:
                needs_adding.append(entity)
        # make all targetProfile lists
        for entity_name, entity in config_['entities'].items():
            if 'links' in entity:
                for link_name, link in entity['links'].items():
                    if isinstance(link['targetProfile'], str):
                        link['targetProfile'] = [link['targetProfile']]
        # add entities that not are sub-typed
        for entity in needs_adding:
            config_['entities'][entity['id']] = entity

        model = Model.parse_obj(config_)
        return model


def initialize_model(config_path):
    """Build the model."""
    model_ = Model.parse_file(config_path, )
    # model_.dependency_order = list(yaml.safe_load(open(config_path))['entities'])
    return model_


class Emitter(BaseModel, abc.ABC):
    """Writes context to a directory."""

    work_dir: str
    """Directory|File to write to."""
    open_files: Dict[str, IO] = {}
    """Open files with lookup key."""
    model: Model
    """our config"""
    anonymizer: dict
    """anonymizer config"""

    class Config:
        """Allow arbitrary user types for fields (since we have reference to io.IOBase)."""

        arbitrary_types_allowed = True

    def __init__(self, **data):
        """Ensure output_path exists."""
        super().__init__(**data)
        pathlib.Path(self.work_dir).mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Close any open files."""

        for file_path, file in self.open_files.items():
            file.close()

    @abc.abstractmethod
    def emit(self, context: fhirclient.models.fhirabstractresource) -> bool:
        """Lookup or open file, write context to file."""
        pass


class DictionaryEmitter(Emitter):
    """Writes gen3 schema elements."""

    STRING_TYPES: ClassVar[List[str]] = ['code', 'uri', 'url', 'canonical', 'xhtml', 'date', 'instant', 'id',
                                         'markdown', 'base64Binary', 'string', 'dateTime', 'String', 'Code', 'DateTime',
                                         'str', 'FHIRDate']
    NUMBER_TYPES: ClassVar[List[str]] = ['decimal', 'positiveInt', 'integer', 'Decimal', 'Integer', 'int', 'float']

    BOOLEAN_TYPES: ClassVar[List[str]] = ['boolean', 'Boolean', 'bool']

    ALL_MAPPED_TYPES: ClassVar[List[str]] = STRING_TYPES + NUMBER_TYPES + BOOLEAN_TYPES

    template: dict
    """gen3 template"""

    _value_sets: object = PrivateAttr()
    _path: str or None = PrivateAttr()
    _seen_already: set = PrivateAttr()
    _redacted_classes: set = PrivateAttr()

    class Config:
        """Allow arbitrary user types for fields (since we have reference to dict)."""

        arbitrary_types_allowed = True

    def __init__(self, **data):
        """Append /gen3 to output_path, init template."""
        data["work_dir"] = data["work_dir"] + "/gen3"
        super().__init__(**data)
        assert self.template
        # self._value_sets = ValueSets()
        self._path = None
        self._seen_already = set()
        # see https://github.com/beda-software/fhirpath-py
        self._redacted_classes = set([r['path'] for r in self.anonymizer['fhirPathRules'] if r['method'] == 'redact'])

    def emit(self, resource: fhirclient.models.fhirabstractresource) -> bool:
        if resource.__class__ not in self._seen_already:
            schema_ = self.render_schema(
                template=self.template,
                model=self.model,
                resource_type=resource.__class__)
            if schema_:
                path = f'{self.work_dir}/{type(resource).__name__}.yaml'
                self.open_files[path] = open(path, "w")
                yaml.dump(
                    schema_,
                    self.open_files[path])
                self.open_files[path].flush()
                self._seen_already.add(resource.__class__)
        return True

    def render_schema(self, template, resource_type, model):
        """Render context into a gen3 schema.

        :param template: - dict  gen3 template
        :param resource_type - class of fhir object
        :param model - config"""
        resource = resource_type()
        if resource_type.__name__ not in model.entities.keys():
            logging.getLogger().info(f"No config for {resource.resource_type}")
            return None
        schema_ = deepcopy(template)
        entity = model.entities[resource_type.__name__]
        schema_['id'] = entity.id
        schema_['title'] = entity.id
        schema_['category'] = entity.category
        schema_['description'] = resource.__doc__ + f" http://hl7.org/fhir/{entity.id.lower()}"
        schema_['links'] = [link for link in self.render_links(entity=entity)]
        schema_['required'] = [_normalize_property_name(required) for required in
                               self.render_required(resource=resource)]
        for property_name, schema_property in self.render_property(entity=entity, resource=resource):
            schema_['properties'][property_name] = schema_property

        if entity.id == 'DocumentReference':
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
            snippet = yaml.load(snippet, Loader=yaml.SafeLoader)
            for k, v in snippet.items():
                schema_['properties'][k] = v

        return schema_

    @staticmethod
    def render_links(entity) -> Iterator[dict]:
        """Gen3 link collection.
        :param: entity from config model"""
        for link_key, link in entity.links.items():
            if link.ignore:
                continue
            _neighbors = link.targetProfile
            if not isinstance(_neighbors, list):
                _neighbors = [_neighbors]
            for _neighbor in _neighbors:
                target_type = _neighbor.split('/')[-1]
                # ent = inflection.camelize(entity.id, uppercase_first_letter=False)
                backref = f"{link_key}_{inflection.pluralize(entity.id)}"
                # f"{ent}_{inflection.pluralize(link_key)}"
                # inflection.camelize(inflection.pluralize(entity.id),uppercase_first_letter=False)
                # name = inflection.pluralize(target_type)
                yield {
                    'name':  f"{link_key}_{target_type}",  # srcdstassoc  {entity.id}_
                    'backref':  backref,  # dstsrcassoc,
                    'label': link_key,  # label,
                    'target_type': target_type,
                    'multiplicity': 'many_to_many',
                    'required': link.required
                }

    @staticmethod
    def resource_properties(resource) -> Iterator[tuple]:
        """Return fields that are mandatory.
        :param: resource instantiated entity
        """
        for tuple_ in resource.elementProperties() + [("resource_type", "resource_type", str, False, None, False)]:
            yield tuple_

    def render_required(self, resource) -> Iterator[str]:
        """Return fields that are mandatory.
        :param: resource instantiated entity"""
        for name in ['submitter_id']:
            yield name
        for name, jsname, typ, is_list, of_many, not_optional in self.resource_properties(resource):
            if not_optional:
                yield name

    def flatten_embedded_property(self, name, jsname, typ, is_list, of_many, not_optional, docstrings, depth_counter=0,
                                  parent_name=None):
        """Flatten a complex type."""
        # maximum depth, prevent RecursionError
        if depth_counter == 3:
            return
        depth_counter += 1

        # ignore anything that is redacted
        for path in self._redacted_classes:
            if '.' in path:
                continue
            if typ.__name__ in path:
                msg = f"Anonymizer Ignoring expansion of {typ.__name__}"
                if first_occurrence(msg):
                    logger.warning(msg)
                return

        resource = typ()
        # iterate through children

        # max 2
        range_limit = 2
        if is_list:
            range_limit = 1

        for list_counter in range(range_limit):
            is_first = True
            for c_name, c_jsname, c_typ, c_is_list, c_of_many, c_not_optional in self.resource_properties(resource):
                # TODO add to model config
                if c_name in ['id', 'resource_type']:
                    continue
                # TODO add to model config
                if typ.__name__ == 'Identifier' and c_name not in ['system', 'value', 'use']:
                    continue
                # TODO add to model config
                if typ.__name__ == 'Reference' and c_name not in ['reference', 'type']:
                    continue

                if c_name in self.model.ignored_properties:
                    continue
                c_docstring = ''
                if c_name in resource.attribute_docstrings():
                    c_docstring = resource.attribute_docstrings()[c_name]
                c_docstrings = [c_docstring]
                if is_first:
                    is_first = False
                    c_docstrings = docstrings + c_docstrings

                if parent_name:
                    property_name = parent_name
                else:
                    property_name = name
                if is_list:
                    property_name = f"{property_name}_{list_counter}_{c_name}"
                else:
                    property_name = f"{property_name}_{c_name}"

                if c_typ.__name__ not in DictionaryEmitter.ALL_MAPPED_TYPES:
                    # msg = f"Expand child {c_name} {c_typ.__name__} parent {typ.__name__}"
                    # if first_occurrence(msg):
                    #     logger.warning(msg)
                    # expand embedded type
                    for expanded in self.flatten_embedded_property(c_name, c_jsname, c_typ, c_is_list, c_of_many,
                                                                   c_not_optional, c_docstrings, depth_counter,
                                                                   parent_name=property_name):
                        yield expanded
                    continue

                schema_property = self.create_schema_property(c_docstrings, c_name, c_not_optional, resource, c_typ)
                yield _normalize_property_name(property_name), schema_property

    def render_property(self, entity, resource) -> tuple[str, dict]:
        """Render the property type and description."""
        for name, jsname, typ, is_list, of_many, not_optional in self.resource_properties(resource):
            # don't generate a property for edges
            if name in entity.links:
                continue
            if name in self.model.ignored_properties:
                continue

            docstring = ''
            # TODO - hierarchy of docstrings?
            if name in resource.attribute_docstrings():
                docstring = resource.attribute_docstrings()[name]
            docstrings = [docstring]

            if typ.__name__ not in DictionaryEmitter.ALL_MAPPED_TYPES:
                # expand embedded type
                for expanded in self.flatten_embedded_property(name, jsname, typ, is_list, of_many, not_optional,
                                                               docstrings):
                    yield expanded
                continue

            schema_property = self.create_schema_property(docstrings, name, not_optional, resource, typ)

            yield _normalize_property_name(name), schema_property

    @staticmethod
    def create_schema_property(docstrings, name, not_optional, resource, typ):

        type_codes = [DictionaryEmitter.normalize_type(typ.__name__, property_name=name,
                                                       resource_type=resource.__class__.__name__)]
        if not not_optional:
            type_codes.append('null')
        schema_property = {
            'type': type_codes, 'description': '. '.join(docstrings)
        }
        if name in resource.attribute_enums():
            property_enum = AttributeEnum(**resource.attribute_enums()[name])

            description = '. '.join(docstrings + [property_enum.url])
            term_def = DictionaryEmitter.get_term_def(property_enum)
            schema_property = {
                'type': type_codes,
                'description': description,
                'term': {'termDef': term_def, 'description': description}
            }
            # add enum

            enum_codes = property_enum.restricted_to
            if enum_codes and property_enum.binding_strength in ['required', 'preferred']:
                del schema_property['type']
                schema_property['enum'] = enum_codes
            elif enum_codes:
                schema_property['description'] = schema_property['description'] + ' ' + '|'.join(enum_codes)
                schema_property['term']['description'] = schema_property['description']
            else:
                logger.debug(f"No enumeration found for: {name} {property_enum.url}")
        return schema_property

    @staticmethod
    def get_term_def(property_enum) -> dict:
        """Cast to json schema types."""
        value_set_id = property_enum.url
        value_set_version = None
        if '|' in property_enum.url:
            value_set_version = value_set_id.split('|')[-1]
        term_def = {
            'term': value_set_id,
            'source': 'fhir',
            'cde_id': value_set_id,
            'cde_version': value_set_version,
            'term_url': value_set_id,
            'strength': property_enum.binding_strength
        }
        return term_def

    @staticmethod
    def primitive_types(code, property_name, resource_type) -> List[str]:
        pass

    @staticmethod
    def normalize_type(code, property_name, resource_type) -> str:
        """Cast to json schema types."""
        if code in FHIR_TYPES:
            return FHIR_TYPES[code].json_type
        if code in DictionaryEmitter.STRING_TYPES:
            return 'string'
        if code in DictionaryEmitter.NUMBER_TYPES:
            return "number"
        if code in DictionaryEmitter.BOOLEAN_TYPES:
            return 'boolean'
        msg = f"No mapping for {code} default to string {resource_type}.{property_name}"
        if first_occurrence(msg):
            logger.warning(msg)
        return 'string'

    @staticmethod
    def description(property_) -> List[str]:
        """Concatenates descriptions (utility)."""
        # TODO - should we concatenate hierarchy of doc strings?
        if property_.docstring:
            return [property_.docstring]
        return ['']


# from collections.abc import Mapping, Set, Sequence
#
# string_types = (str, unicode) if str is bytes else (str, bytes)
# iteritems = lambda mapping: getattr(mapping, 'iteritems', mapping.items)()
#
#
# def objwalk(obj, path=(), match=None, memo=None, depth=0, max_depth=4):
#     """Walk an object tree, return any item that matches.
#     :param: obj item to walk
#     :param: match function will be passed path and obj, return true to yield item."""
#     if memo is None:
#         memo = set()
#     if match and match(path, obj):
#         yield path, obj, obj.__class__
#     depth += 1
#     if depth == max_depth:
#         return
#     iterator = None
#     if hasattr(obj, '__dict__'):
#         obj = vars(obj)
#     if isinstance(obj, Mapping):
#         iterator = iteritems
#     elif isinstance(obj, (Sequence, Set)) and not isinstance(obj, string_types):
#         iterator = enumerate
#     if iterator:
#         if id(obj) not in memo:
#             memo.add(id(obj))
#             for path_component, value in iterator(obj):
#                 for result in objwalk(value, path + (path_component,), memo=memo, match=match, depth=depth):
#                     yield result
#             memo.remove(id(obj))


class TransformerEmitter(Emitter):
    _data_dictionary: dict = PrivateAttr()
    _seen_already: List[str] = PrivateAttr()

    def __init__(self, **data):
        """Append /extractions to output_path"""
        data["work_dir"] = data["work_dir"] + "/extractions"
        super().__init__(**data)
        self._data_dictionary = data["data_dictionary"]
        self._seen_already = []

    def emit(self, resource: fhirclient.models.fhirabstractresource) -> bool:
        if type(resource).__name__ not in self.model.entities:
            msg = f"No mapping for {type(resource).__name__} skipping."
            if first_occurrence(msg):
                logger.warning(msg)
            return True

        # determine links and extract any embedded objects
        links = self.process_links(resource)

        # emit the object
        path = f'{self.work_dir}/{type(resource).__name__}.ndjson'
        if path not in self.open_files:
            self.open_files[path] = open(path, "w")

        resource_model = self._data_dictionary[f"{type(resource).__name__}.yaml"]
        flattened = {k: v for k, v in flatten(resource.as_json(), separator='_').items() if
                     k in resource_model['properties'].keys()}
        id_ = resource.id
        json.dump(
            {
                'id': id_,
                "name": type(resource).__name__,
                'relations': [lnk.dict() for lnk in links],
                'object': flattened
            },
            self.open_files[path])
        self.open_files[path].write('\n')

    def extract_resource(self, resource, link) -> List[LinkInstance]:
        """Emit embedded resource."""
        extracted_resources = getattr(resource, link.id)
        if not extracted_resources:
            return
        if not isinstance(extracted_resources, list):
            extracted_resources = [extracted_resources]
        for extracted_resource in extracted_resources:
            extracted_class = type(extracted_resource)

            extracted_class_name = extracted_class.__name__
            path = f'{self.work_dir}/{extracted_class_name}.ndjson'
            if path not in self.open_files:
                self.open_files[path] = open(path, "w")
            id_ = None
            if extracted_class_name == 'Coding':
                id_ = f"{extracted_resource.system}?code={extracted_resource.code}"
                if extracted_resource.version:
                    id_ = f"{id_}&{extracted_resource.version}"
            elif extracted_class_name == 'CodeableConcept':
                ids = []
                if extracted_resource.coding:
                    for coding in extracted_resource.coding:
                        id_ = f"{coding.system}?code={coding.code}"
                        if coding.version:
                            id_ = f"{id_}&{coding.version}"
                        ids.append(id_)
                else:
                    ids.append(extracted_resource.text)
                id_ = str(uuid.uuid3(ACED_CODEABLE_CONCEPT, '~'.join(ids)))
            assert id_, "We should have calculated an id"
            extracted_resource.id = id_
            resource_model = self._data_dictionary[f"{type(extracted_resource).__name__}.yaml"]
            flattened = {k: v for k, v in flatten(extracted_resource.as_json(), separator='_').items() if
                         k in resource_model['properties'].keys()}

            if id_ not in self._seen_already:
                object_ = extracted_resource.as_json()
                json.dump(
                    {
                        'id': id_,
                        'object': flattened,
                        "relations": [],
                        "name": type(extracted_resource).__name__,
                    },
                    self.open_files[path]
                )
                self.open_files[path].write('\n')
                self._seen_already.append(id_)
            yield LinkInstance(dst_id=id_, dst_name=extracted_class_name, label=link.id)

    def process_links(self, resource) -> List[LinkInstance]:
        """Emits any embedded objects, returns a list of links."""
        links = self.model.entities[type(resource).__name__].links.values()
        links_to_return = []
        for link in links:
            if link.ignore:
                continue
            if len([tp for tp in link.targetProfile if tp.split('/')[-1] in ['Coding', 'CodeableConcept']]) > 0:
                links_to_return.extend(self.extract_resource(resource, link))
                continue
            link_id = link.id
            if link_id not in vars(resource):
                link_id = link_id + "_fhir"
            if link_id not in vars(resource):
                msg = f"Could not find {link.id} in {type(resource).__name__} skipping."
                if first_occurrence(msg):
                    logger.warning(msg)
                    continue
            extracted_resources = getattr(resource, link_id)
            if not extracted_resources:
                continue
            if not isinstance(extracted_resources, list):
                extracted_resources = [extracted_resources]
            for er in extracted_resources:
                if hasattr(er, 'valueReference') and getattr(er, 'valueReference'):
                    er = getattr(er, 'valueReference')
                if type(er) == FHIRReference:
                    dst_id = er.reference.split('/')[-1]
                    dst_name = er.reference.split('/')[0]
                else:
                    dst_id = er.id
                    dst_name = type(er).__name__
                if not dst_id:
                    print("?")
                links_to_return.append(LinkInstance(dst_id=dst_id, dst_name=dst_name, label=link.id))
        return links_to_return


@click.group()
def cli():
    """Manage data model schema and transforms."""
    pass


@cli.group()
def schema():
    """Manage schema."""
    pass


@cli.group()
def config():
    """Manage config."""
    pass


@cli.group()
def data():
    """Manage data."""
    pass


@config.command(name='introspect')
@click.option('--config_path',
              default='config.yaml',
              show_default=True,
              help='Path to config file.')
def config_introspect(config_path):
    """Improve config by introspecting and adding links to 'Reference', 'CodeableConcept', 'Coding'."""
    model = initialize_model(config_path=config_path)
    for entity_name in model.entities:
        module_name = f"fhirclient.models.{entity_name.lower()}"
        module = importlib.import_module(module_name)
        clazz = getattr(module, entity_name)
        assert clazz
        for name, jsname, typ, is_list, of_many, not_optional in clazz().elementProperties():
            if typ.__name__ in ['FHIRReference', 'CodeableConcept', 'Coding']:
                # if name not in model.entities[entity_name].links:
                target_profile_name = typ.__name__
                ignore = False
                if typ.__name__ == 'FHIRReference':
                    target_profile_name = '%UNKNOWN%'
                    ignore = True
                else:
                    if target_profile_name not in model.entities:
                        logger.warning(f"{target_profile_name} referenced from {name} not in config")
                # if name == 'type':
                #     ignore = True
                #     logger.warning(
                #         f"# TODO - graphql error from peregrine if we have a link with name 'type' see {entity_name}?")
                target_profile = [f"http://hl7.org/fhir/StructureDefinition/{target_profile_name}"]
                model.entities[entity_name].links[name] = Link(id=name, required=False, targetProfile=target_profile,
                                                               ignore=ignore)
    dict_ = model.dict()
    for entity_name, entity in dict_['entities'].items():
        # remove redundant keys
        del entity['submitter_id']
        for link in entity['links'].values():
            del link['id']
    print(yaml.dump(dict_))


@schema.command(name="generate")
@click.option('--file_name_pattern',
              default='research_study*.json',
              show_default=True,
              help='File names to match.')
@click.option('--input_path',
              default='output/',
              show_default=True,
              help='Path to output data dir.')
@click.option('--config_path',
              default='config.yaml',
              show_default=True,
              help='Path to config file.')
@click.option('--anonymizer_config_path',
              default='anonymizer/hippa.yaml',
              show_default=True,
              help='Path to config file.')
def schema_generate(input_path, file_name_pattern, config_path, anonymizer_config_path):
    """Parse config, create dictionary schemas."""

    # validate parameters
    assert os.path.isdir(input_path)
    input_path = Path(input_path)
    assert os.path.isdir(input_path)
    file_paths = list(input_path.glob(file_name_pattern))
    assert len(
        file_paths) >= 1, f"{str(input_path)}/{file_name_pattern} only returned {len(file_paths)} expected at least 1"
    model = initialize_model(config_path=config_path)

    path = 'scripts/gen3_schema_template.yaml'  # Do not use os.path.join()
    template = yaml.load(open(path), Loader=yaml.SafeLoader)
    anonymizer = yaml.load(open(anonymizer_config_path), Loader=yaml.SafeLoader)
    emitter = DictionaryEmitter(template=template, model=model, work_dir='output/', anonymizer=anonymizer)

    for entity_name in model.entities:
        module_name = f"fhirclient.models.{entity_name.lower()}"
        module = importlib.import_module(module_name)
        clazz = getattr(module, entity_name)
        assert clazz
        emitter.emit(clazz())

    emitter.close()


@schema.command(name='compile')
@click.argument('schema_path', default="output/gen3")
@click.option('--out', default="aced-test.json", help="Output path")
def schema_compile(schema_path, out):
    """Converts yaml files to json schema."""
    schema_path = Path(schema_path)
    assert schema_path.is_dir(), f"{schema_path} should be a path"
    click.echo(f"Writing schema into {out}...")
    with open(out, "w") as f:
        json.dump(dump_schemas_from_dir(schema_path), f)


@schema.command(name='publish')
@click.argument('dictionary_path', default='aced-test.json')
@click.option('--bucket', default="s3://aced-public", help="Bucket target")
def schema_publish(dictionary_path, bucket):
    """Copy dictionary to s3 (note:aws cli dependency)"""

    dictionary_path = Path(dictionary_path)
    assert dictionary_path.is_file(), f"{dictionary_path} should be a path"
    click.echo(f"Writing schema into {bucket}")
    import subprocess
    cmd = f"aws s3 cp {dictionary_path} {bucket}".split(' ')
    s3_cp = subprocess.run(cmd)
    assert s3_cp.returncode == 0, s3_cp
    print("OK")


@schema.command(name='tables')
@click.option('--dictionary_path',
              default='output/gen3',
              show_default=True,
              help='Gen3 yaml files')
def schema_tables(dictionary_path, dictionary_url=None):
    """Show dictionary to postgres table mappings."""

    mapping = _table_mappings(dictionary_path, dictionary_url)

    print(json.dumps([mapping for mapping in mapping]))


def _table_mappings(dictionary_path, dictionary_url):
    """Gen3 vertex/edge table mappings."""
    _dictionary, model = _init_dictionary(root_dir_=dictionary_path, dictionary_url=dictionary_url)
    ddt = DataDictionaryTraversal(model)
    desired_keys = [
        '__dst_class__',
        '__dst_src_assoc__',
        '__dst_table__',
        '__label__',
        '__src_class__',
        '__src_dst_assoc__',
        '__src_table__',
        '__tablename__'
    ]

    def _transform(ddt_) -> List[dict]:
        for d in ddt_.get_edges():
            yield {k.replace('_', ''): v for k, v in d.__dict__.items() if k in desired_keys}

    mapping = _transform(ddt)
    return mapping


@data.command(name='transform')
@click.option('--file_name_pattern',
              default='research_study*.json',
              show_default=True,
              help='File names to match.')
@click.option('--input_path',
              default='output/',
              show_default=True,
              help='Path to output data dir.')
@click.option('--config_path',
              default='config.yaml',
              show_default=True,
              help='Path to config file.')
@click.option('--anonymizer_config_path',
              default='anonymizer/hippa.yaml',
              show_default=True,
              help='Path to config file.')
@click.option('--dictionary_path',
              default='aced-test.json',
              show_default=True,
              help='Path to data dictionary file.')
def data_transform(input_path, file_name_pattern, config_path, anonymizer_config_path, dictionary_path):
    """Transform from FHIRBundles to Graph."""

    # validate parameters
    assert os.path.isdir(input_path)
    input_path = Path(input_path)
    assert os.path.isdir(input_path)
    file_paths = list(input_path.glob(file_name_pattern))
    assert len(
        file_paths) >= 1, f"{str(input_path)}/{file_name_pattern} only returned {len(file_paths)} expected at least 1"
    model = initialize_model(config_path=config_path)

    # path = 'scripts/gen3_schema_template.yaml'  # Do not use os.path.join()
    # template = yaml.load(open(path), Loader=yaml.SafeLoader)
    anonymizer = yaml.load(open(anonymizer_config_path), Loader=yaml.SafeLoader)
    data_dictionary = json.load(open(dictionary_path))

    emitters = [
        # ExtractorEmitter(template=template, model=model, work_dir='output/', anonymizer=anonymizer),
        TransformerEmitter(model=model, work_dir='output/', anonymizer=anonymizer, data_dictionary=data_dictionary)
    ]

    already_added = []
    for file_path in file_paths:
        bundle = Bundle(json.load(open(file_path)))
        research_subjects = [bundle_entry.resource for bundle_entry in bundle.entry if
                             bundle_entry.resource.resource_type == "ResearchSubject"]
        sources = sorted(research_subject.meta.source for research_subject in research_subjects if
                         research_subject.meta.source not in already_added)
        # sources = sources[:4]  # truncate for testing
        sources.append(file_path)  # add study bundle
        for source in sources:
            for bundle_entry in Bundle(json.load(open(source))).entry:
                for emitter in emitters:
                    emitter.emit(bundle_entry.resource)
            already_added.append(source)
        break  # only first study for testing

    for emitter in emitters:
        emitter.close()

    return True


def load_vertices(files, connection, model, project_id, mapping):
    """Load files into database vertices."""
    logger.info(f"Number of files available for load: {len(files)}")
    for entity_name in model.dependency_order:
        path = next(iter([fn for fn in files if str(fn).endswith(f"{entity_name}.ndjson")]), None)
        if not path:
            logger.warning(f"No file found for {entity_name} skipping")
            continue
        data_table_name = next(
            iter(
                set([m['dsttable'] for m in mapping if m['dstclass'].lower() == entity_name.lower()] +
                    [m['srctable'] for m in mapping if m['srcclass'].lower() == entity_name.lower()])
            ),
            None)
        if not data_table_name:
            logger.warning(f"No mapping found for {entity_name} skipping")
            continue
        logger.info(f"loading {path} into {data_table_name}")

        with connection.cursor() as cursor:
            with open(path) as f:
                # copy a block of records into a file like stringIO buffer
                record_count = 0
                for lines in chunk(f.readlines(), 100):
                    buf = io.StringIO()
                    for line in lines:
                        record_count += 1
                        d_ = json.loads(line)
                        d_['object']['project_id'] = project_id
                        obj_str = json.dumps(d_['object'])
                        csv = f"{d_['id']}|{obj_str}|{{}}|{{}}|{datetime.now()}".replace('\n', '\\n').replace("\\","\\\\")
                        if d_['id'] == "3452ba4b-b85e-4175-d139-4b453e1bfddb":
                            print("?")
                        csv = csv + '\n'
                        buf.write(csv)
                    buf.seek(0)
                    # efficient way to write to postgres
                    cursor.copy_from(buf, data_table_name, sep='|', columns=['node_id', '_props', 'acl', '_sysan', 'created'])
                    logger.info(f"wrote {record_count} records to {data_table_name} from {path}")
        connection.commit()


def load_edges(files, connection, model, project_id, mapping, project_node_id):
    """Load files into database edges."""
    logger.info(f"Number of files available for load: {len(files)}")
    for entity_name in model.dependency_order:
        path = next(iter([fn for fn in files if str(fn).endswith(f"{entity_name}.ndjson")]), None)
        if not path:
            logger.warning(f"No file found for {entity_name} skipping")
            continue

        with connection.cursor() as cursor:
            with open(path) as f:
                # copy a block of records into a file like stringIO buffer
                record_count = 0
                for lines in chunk(f.readlines(), 100):
                    buffers = defaultdict(io.StringIO)
                    for line in lines:
                        d_ = json.loads(line)
                        relations = d_['relations']
                        if d_['name'] == 'ResearchStudy':
                            # link the ResearchStudy to the gen3 project
                            relations.append({"dst_id": project_node_id, "dst_name": "project", "label": "project"})

                        if len(relations) == 0:
                            continue
                        record_count += 1
                        for relation in relations:
                            # get destination table
                            edge_table_mapping = next(
                                iter(
                                    [
                                        m for m in mapping
                                        if m['srcclass'].lower() == entity_name.lower()
                                        and m['label'].lower() == relation['label'].lower()
                                        and m['dstclass'].lower() == relation['dst_name'].lower()
                                     ]
                                ),
                                None
                            )
                            if not edge_table_mapping:
                                msg = f"No mapping for src {entity_name} dst {relation['dst_name']} label {relation['label']}"
                                logger.error(msg)
                                raise Exception(msg)
                            table_name = edge_table_mapping['tablename']
                            buf = buffers[table_name]
                            # src_id | dst_id | acl | _sysan | _props | created |
                            buf.write(f"{d_['id']}|{relation['dst_id']}|{{}}|{{}}|{{}}|{datetime.now()}\n")
                    for table_name, buf in buffers.items():
                        buf.seek(0)
                        # efficient way to write to postgres
                        cursor.copy_from(buf, table_name, sep='|',
                                         columns=['src_id', 'dst_id', 'acl', '_sysan', '_props', 'created'])
                        logger.info(f"wrote {record_count} records to {table_name} from {path}")
        connection.commit()


@data.command(name='load')
@click.option('--file_name_pattern',
              default='*.ndjson',
              show_default=True,
              help='File names to match.')
@click.option('--input_path',
              default='output/extractions',
              show_default=True,
              help='Path to transformed data.')
@click.option('--sheepdog_creds_path',
              default='../compose-services/Secrets/sheepdog_creds.json',
              show_default=True,
              help='Path to sheepdog credentials.')
@click.option('--program_name',
              default='MyFirstProgram',
              show_default=True,
              help='Existing program in Gen3.')
@click.option('--project_code',
              default='MyFirstProject',
              show_default=True,
              help='Existing project in Gen3.')
@click.option('--db_host',
              default=None,
              show_default=True,
              help='Connect to db using this host')
@click.option('--config_path',
              default='config.yaml',
              show_default=True,
              help='Path to config file.')
@click.option('--dictionary_path',
              default=None,
              show_default=True,
              help='Path to data dictionary file.')
@click.option('--dictionary_url',
              default='https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json',
              show_default=True,
              help='Data dictionary url.')
def data_load(input_path, file_name_pattern, sheepdog_creds_path, program_name, project_code, db_host, config_path,
              dictionary_path, dictionary_url):
    """Load transformed data to postgres."""
    # check config
    model = initialize_model(config_path=config_path)
    # check db connection
    sheepdog_creds = json.load(open(sheepdog_creds_path))
    db_username = sheepdog_creds['db_username']
    db_password = sheepdog_creds['db_password']
    db_database = sheepdog_creds['db_database']
    if not db_host:
        db_host = sheepdog_creds['db_host']
    conn = psycopg2.connect(
        database=db_database,
        user=db_username,
        password=db_password,
        host=db_host,
        # port=DATABASE_CONFIG.get('port'),
    )
    assert conn
    print("Connected to postgres")
    # check program/project exist
    cur = conn.cursor()
    cur.execute("select node_id, _props from \"node_program\";")
    programs = cur.fetchall()
    programs = [{'node_id': p[0], '_props': p[1]} for p in programs]
    program = next(iter([p for p in programs if p['_props']['name'] == program_name]), None)
    assert program, f"{program_name} not found in node_program table"
    cur.execute("select node_id, _props from \"node_project\";")
    projects = cur.fetchall()
    projects = [{'node_id': p[0], '_props': p[1]} for p in projects]
    project_node_id = next(iter([p['node_id'] for p in projects if p['_props']['code'] == project_code]), None)
    assert project_node_id, f"{project_code} not found in node_project"
    project_id = f"{program_name}-{project_code}"
    print(f"Program and project exist: {project_id}")
    # check files
    input_path = Path(input_path)
    assert input_path.is_dir(), f"{input_path} should be a directory"
    files = [fn for fn in input_path.glob(file_name_pattern)]
    assert len(files) > 0, f"No files found at {input_path}/{file_name_pattern}"
    # check the mappings
    mappings = [mapping for mapping in _table_mappings(dictionary_path, dictionary_url)]
    # load the files
    logger.info("Loading vertices")
    load_vertices(files, conn, model, project_id, mappings)
    logger.info("Loading edges")
    load_edges(files, conn, model, project_id, mappings, project_node_id)
    logger.info("Done")


if __name__ == '__main__':
    cli()
