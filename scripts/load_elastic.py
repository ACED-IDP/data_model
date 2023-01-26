import csv
import datetime
import json
import logging
from datetime import datetime
from typing import Iterator, Dict

import click
import elasticsearch
import requests
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from more_itertools import peekable
from dictionaryutils import DataDictionary
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger('elasticsearch').setLevel(logging.WARNING)

DEFAULT_ELASTIC = "http://localhost:9200"
DEFAULT_NAMESPACE = "gen3.aced.io"

ACED_NAMESPACE = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced-ipd.org')


def create_id(key: str) -> str:
    """Create an idempotent ID from the input string."""
    return str(uuid.uuid5(ACED_NAMESPACE, key))


def read_ndjson(path: str) -> Iterator[Dict]:
    """Read ndjson file, load json line by line."""
    with open(path) as jsonfile:
        for l_ in jsonfile.readlines():
            yield json.loads(l_)


def read_tsv(path: str) -> Iterator[Dict]:
    """Read tsv file line by line."""
    with open(path) as tsv_file:
        reader = csv.DictReader(tsv_file, delimiter="\t")
        for row in reader:
            yield row


def create_index_from_source(_schema, _index, _type):
    """Given an ES source dict, create ES index."""
    mappings = {}
    if _type == 'file':
        # TODO fix me - we should have a index called document_reference not file
        properties = _schema['document_reference']['properties']
    else:
        properties = _schema[_type]['properties']
    for k, v in properties.items():
        if '$ref' in v:
            (ref_, ref_prop) = v['$ref'].split('#/')
            prop_type = _schema[ref_][ref_prop]['type']
        elif 'enum' in v:
            prop_type = 'string'
        elif 'oneOf' in v:
            prop_type = 'string'
        else:
            if 'type' not in v:
                print("?")
            if isinstance(v['type'], list):
                prop_type = v['type'][0]
            else:
                prop_type = v['type']

        if prop_type in ['string']:
            mappings[k] = {
                "type": "keyword"
            }
        elif prop_type in ['boolean']:
            mappings[k] = {
                "type": "keyword"
            }
        elif 'date' in prop_type:
            mappings[k] = {
                "type": "date"
            }
        else:
            # naive, there are probably other types
            mappings[k] = {"type": "float"}
        # we have a patient centric index approach, all links include a `patient`
        mappings['patient_id'] = {"type": "keyword"}
    return {
        # https://www.elastic.co/guide/en/elasticsearch/reference/current/dynamic.html#dynamic-parameters
        "mappings": {_type: {"properties": mappings}}
    }


def write_array_aliases(doc_type, alias, elastic=DEFAULT_ELASTIC, name_space=DEFAULT_NAMESPACE):
    """Write the array aliases."""
    # EXPECTED_ALIASES = {
    #     ".kibana_1": {
    #         "aliases": {
    #             ".kibana": {}
    #         }
    #     },
    #     "etl-array-config_0": {
    #         "aliases": {
    #             "etl-array-config": {},
    #             "etl_array-config": {},
    #             "time_2022-08-25T01:44:47.115494": {}
    #         }
    #     },
    #     "etl_0": {
    #         "aliases": {
    #             "etl": {},
    #             "time_2022-08-25T01:44:47.115494": {}
    #         }
    #     },
    #     "file-array-config_0": {
    #         "aliases": {
    #             "file-array-config": {},
    #             "file_array-config": {},
    #             "time_2022-08-25T01:44:47.115494": {}
    #         }
    #     },
    #     "file_0": {
    #         "aliases": {
    #             "file": {},
    #             "time_2022-08-25T01:44:47.115494": {}
    #         }
    #     }
    # }
    return {
        "method": 'POST',
        "url": f'{elastic}/_aliases',
        "json": {
            "actions": [
                {"add": {"index": f"{name_space}_{doc_type}-array-config_0",
                         "alias": f"{name_space}_array-config"}},
                {"add": {"index": f"{name_space}_{doc_type}-array-config_0",
                         "alias": f"{alias}_array-config"}}
            ]}
    }


def write_array_config(doc_type, alias, field_array, elastic=DEFAULT_ELASTIC, name_space=DEFAULT_NAMESPACE):
    """Write the array config."""
    return {
        "method": 'PUT',
        "url": f'/{name_space}_{doc_type}-array-config_0/_doc/{alias}',
        "json": {"timestamp": datetime.now().isoformat(), "array": field_array}
    }


def write_alias_config(doc_type, alias, elastic=DEFAULT_ELASTIC, name_space=DEFAULT_NAMESPACE):
    """Write the alias config."""
    return {
        "method": 'POST',
        "url": f'{elastic}/_aliases',
        "json": {"actions": [{"add": {"index": f"{name_space}_{doc_type}_0", "alias": alias}}]}
    }


def create_indexes(_schema, _index, doc_type, elastic=DEFAULT_ELASTIC):
    """Create the es indexes."""
    return {
        "method": 'PUT',
        "url": f'{elastic}/{_index}',
        "json": create_index_from_source(_schema, _index, doc_type),
        "index": _index,
        "type": doc_type
    }


def write_bulk_http(elastic, index, limit, doc_type, generator, schema):
    """Use efficient method to write to elastic"""
    counter = 0

    def _bulker(generator_, counter_=counter):
        for dict_ in generator_:
            if limit and counter_ > limit:
                break  # for testing
            yield {
                '_index': index,
                '_op_type': 'index',
                '_type': doc_type,
                '_source': dict_
            }
            counter_ += 1
            if counter_ % 10000 == 0:
                logger.info(f"{counter_} records written")
        logger.info(f"{counter_} records written")

    logger.info(f'Creating {doc_type} indices.')
    index_dict = create_indexes(schema, _index=index, doc_type=doc_type)

    try:
        elastic.indices.create(index=index_dict['index'], body=index_dict['json'])
    except Exception as e:
        logger.warning(f"Could not create index. {index} {str(e)}")
        logger.warning("Continuing to load.")

    logger.info(f'Writing bulk to {index} limit {limit}.')
    _ = bulk(client=elastic,
             actions=(d for d in _bulker(generator)),
             request_timeout=120)


def observation_generator(project_id, path) -> Iterator[Dict]:
    """Render guppy index for observation."""
    program, project = project_id.split('-')
    for observation in read_ndjson(path):
        o_ = observation['object']

        o_['project_id'] = project_id
        o_["auth_resource_path"] = f"/programs/{program}/projects/{project}"
        for relation in observation['relations']:
            dst_name = relation['dst_name'].lower()
            dst_id = relation['dst_id']
            o_[f'{dst_name}_id'] = dst_id

        # # synthetic encounter
        # o_["encounter_id"] = create_id(o_['patient_id'] + "encounter_id")
        # o_["encounter_type"] = "General examination of patient (procedure)"
        # o_["encounter_reason"] = 'synthetic'  # TODO
        #
        # # rename a few fields for guppy's display
        # o_["observation_id"] = o_['id']
        # # o_["bodySite"] = o_['bodySite_coding_0_code']

        #
        for required_field in []:
            if required_field not in o_:
                o_[required_field] = None
        yield o_


def patient_generator(project_id, path) -> Iterator[Dict]:
    """Render guppy index for patient."""
    program, project = project_id.split('-')
    for patient in read_ndjson(path):
        p_ = patient['object']
        p_['id'] = patient['id']

        p_['project_id'] = project_id
        p_["auth_resource_path"] = f"/programs/{program}/projects/{project}"

        #
        for required_field in []:
            if required_field not in p_:
                p_[required_field] = None
        yield p_


def file_generator(project_id, path) -> Iterator[Dict]:
    """Render guppy index for file."""
    program, project = project_id.split('-')
    for file in read_ndjson(path):
        f_ = file['object']
        f_['id'] = file['id']

        f_['project_id'] = project_id
        f_["auth_resource_path"] = f"/programs/{program}/projects/{project}"

        for relation in file['relations']:
            dst_name = relation['dst_name'].lower()
            dst_id = relation['dst_id']
            f_[f'{dst_name}_id'] = dst_id

        #
        for required_field in []:
            if required_field not in f_:
                f_[required_field] = None
        yield f_


def setup_aliases(alias, doc_type, elastic, field_array, index):
    """Create the alias to the data index"""
    elastic.indices.put_alias(index, alias)
    # create a configuration index that guppy will read that describes the array fields
    # TODO - find a doc or code reference in guppy that explains how this is used
    alias_index = f'{DEFAULT_NAMESPACE}_{doc_type}-array-config_0'
    try:
        elastic.create(alias_index, id='alias', doc_type='_doc',
                       body={"timestamp": datetime.now().isoformat(), "array": field_array})
    except elasticsearch.exceptions.ConflictError:
        pass
    elastic.indices.update_aliases(
        {"actions": [{"add": {"index": f"{DEFAULT_NAMESPACE}_{doc_type}_0", "alias": alias}}]}
    )
    elastic.indices.update_aliases({
        "actions": [
            {"add": {"index": f"{DEFAULT_NAMESPACE}_{doc_type}-array-config_0",
                     "alias": f"{DEFAULT_NAMESPACE}_array-config"}},
            {"add": {"index": f"{DEFAULT_NAMESPACE}_{doc_type}-array-config_0",
                     "alias": f"{doc_type}_array-config"}}
        ]}
    )


@click.command('elastic')
@click.option('--project_id', required=True,
              default=None,
              show_default=True,
              help='program-project'
              )
@click.option('--index', required=True,
              default=None,
              show_default=True,
              help='Elastic index name'
              )
@click.option('--path', required=True,
              default=None,
              show_default=True,
              help='Path to flattened json'
              )
@click.option('--elastic_url', default=DEFAULT_ELASTIC)
@click.option('--limit',
              default=None,
              show_default=True,
              help='Max number of rows per index.')
@click.option('--dictionary_url',
              default='https://aced-public.s3.us-west-2.amazonaws.com/coherent.gen3.json',
              show_default=True,
              help='Gen3 schema')
def load_elastic(project_id, index, path, limit, elastic_url, dictionary_url):
    """Gen3 elastic search (guppy)."""
    # replaces tube_lite

    if limit:
        limit = int(limit)

    elastic = Elasticsearch([elastic_url], request_timeout=120)

    index = index.lower()

    # schema = requests.get(dictionary_url).json()
    schema = DataDictionary(url=dictionary_url).schema

    if index == 'patient':
        doc_type = 'patient'
        index = f"{DEFAULT_NAMESPACE}_{doc_type}_0"
        alias = 'patient'
        field_array = []

        # create the index and write data into it.
        write_bulk_http(elastic=elastic, index=index, doc_type=doc_type, limit=limit,
                        generator=patient_generator(project_id, path), schema=schema)

        setup_aliases(alias, doc_type, elastic, field_array, index)

    if index == 'observation':
        doc_type = 'observation'
        index = f"{DEFAULT_NAMESPACE}_{doc_type}_0"
        alias = 'observation'
        field_array = ['data_format', 'data_type', '_file_id', 'medications', 'conditions']

        # create the index and write data into it.
        write_bulk_http(elastic=elastic, index=index, doc_type=doc_type, limit=limit,
                        generator=observation_generator(project_id, path), schema=schema)

        setup_aliases(alias, doc_type, elastic, field_array, index)

    if index == 'file':
        doc_type = 'file'
        alias = 'file'
        index = f"{DEFAULT_NAMESPACE}_{doc_type}_0"
        field_array = ["project_code", "program_name"]

        # create the index and write data into it.
        write_bulk_http(elastic=elastic, index=index, doc_type=doc_type, limit=limit,
                        generator=file_generator(project_id, path), schema=schema)

        setup_aliases(alias, doc_type, elastic, field_array, index)


if __name__ == '__main__':
    load_elastic()
