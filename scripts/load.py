#!/usr/bin/env python3

"""Load Gen3."""

import csv
import datetime
import io
import json
import logging
import pathlib
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from itertools import islice
from typing import Dict, Iterator, List

import click
import elasticsearch
import inflection
import jwt
import orjson
import psycopg2
import yaml
from dictionaryutils import DataDictionary, dictionary
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from gen3.auth import Gen3Auth
from gen3.metadata import Gen3Metadata
from gen3.submission import Gen3Submission
from pelican.dictionary import DataDictionaryTraversal
from yaml import SafeLoader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger('elasticsearch').setLevel(logging.WARNING)


DEFAULT_ELASTIC = "http://localhost:9200"
DEFAULT_NAMESPACE = "gen3.aced.io"

ACED_NAMESPACE = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced-ipd.org')

LOGGED_ALREADY = []


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
    # see https://github.com/uc-cdis/guppy/blob/master/src/server/schema.js#L5-L18
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
                "type": "keyword"
            }
        elif 'array' in prop_type:
            mappings[k] = {
                "type": "keyword"
            }
        else:
            # naive, there are probably other types
            mappings[k] = {"type": "float"}
        # we have a patient centric index approach, all links include a `patient`
        mappings['patient_id'] = {"type": "keyword"}
        # patient fields copied to observation
        if _type == 'observation':
            mappings['us_core_race'] = {"type": "keyword"}
            mappings['address'] = {"type": "keyword"}
            mappings['gender'] = {"type": "keyword"}
            mappings['birthDate'] = {"type": "keyword"}
            mappings['us_core_ethnicity'] = {"type": "keyword"}
            mappings['address_orh_zip_designation_code'] = {"type": "keyword"}
            mappings['condition'] = {"type": "keyword"}
            mappings['condition_code'] = {"type": "keyword"}
            mappings['family_history_condition'] = {"type": "keyword"}
            mappings['family_history_condition_code'] = {"type": "keyword"}
            # mappings['effectiveTiming'] = {"type": "date"}
            # mappings['effectiveDateTime'] = {"type": "date"}

            mappings['diastolic_blood_pressure'] = {"type": "keyword"}
            mappings['diastolic_blood_pressure_unit'] = {"type": "keyword"}
            mappings['systolic_blood_pressure'] = {"type": "keyword"}
            mappings['systolic_blood_pressure_unit'] = {"type": "keyword"}
            mappings['predicted_phenotype'] = {"type": "keyword"}
            mappings['predicted_phenotype_coding'] = {"type": "keyword"}

    ## v 7
    # return {
    #     # https://www.elastic.co/guide/en/elasticsearch/reference/current/dynamic.html#dynamic-parameters
    #     "mappings": {"properties": mappings}
    # }

    # v 6
    return {
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
        "type": doc_type  # v 6
    }


def write_sqlite(index, generator):
    """Write to sqlite"""
    connection = sqlite3.connect(f'{index}.sqlite')
    with connection:
        connection.execute(f'DROP table IF EXISTS {index}')
        connection.execute(f'CREATE TABLE if not exists {index} (id PRIMARY KEY, entity Text)')
        with connection:
            connection.executemany(f'insert into {index} values (?, ?)',
                                   [(entity['id'], orjson.dumps(entity).decode(),) for entity in generator])


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
                '_type': doc_type,  # v 6
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
        if 'resource_already_exists_exception' not in str(e):
            logger.warning(f"Could not create index. {index} {str(e)}")
            print(json.dumps(index_dict['json']))
            exit(1)

        # logger.warning("Continuing to load.")

    logger.info(f'Writing bulk to {index} limit {limit}.')
    _ = bulk(client=elastic,
             actions=(d for d in _bulker(generator)),
             request_timeout=120)


def observation_generator(project_id, path) -> Iterator[Dict]:
    """Render guppy index for observation."""
    program, project = project_id.split('-')

    connection = sqlite3.connect(f'denormalized_patient.sqlite')

    for observation in read_ndjson(path):
        o_ = observation['object']

        o_['project_id'] = project_id
        o_["auth_resource_path"] = f"/programs/{program}/projects/{project}"
        for relation in observation['relations']:
            dst_name = relation['dst_name'].lower()
            dst_id = relation['dst_id']
            o_[f'{dst_name}_id'] = dst_id

        #
        for required_field in []:
            if required_field not in o_:
                o_[required_field] = None

        assert 'patient_id' in o_, observation

        denormalized_patient = fetch_denormalized_patient(connection, o_['patient_id'])
        condition, condition_coding, fh_condition, fh_condition_coding, patient = (
            denormalized_patient['condition'],
            denormalized_patient['condition_coding'],
            denormalized_patient['fh_condition'],
            denormalized_patient['fh_condition_coding'],
            denormalized_patient['patient'],
        )

        if patient:
            o_['us_core_race'] = patient.get('us_core_race', None)
            o_['address'] = patient.get('address', None)
            o_['gender'] = patient.get('gender', None)
            o_['birthDate'] = patient.get('birthDate', None)
            o_['us_core_ethnicity'] = patient.get('us_core_ethnicity', None)
            o_['address_orh_zip_designation_code'] = patient.get('address_orh_zip_designation_code', None)

            o_['condition'] = condition
            o_['condition_code'] = condition_coding
            o_['family_history_condition'] = fh_condition
            o_['family_history_condition_code'] = fh_condition_coding

        yield o_


@lru_cache(maxsize=1024*10)
def fetch_denormalized_patient(connection, patient_id):
    """Retrieve unique conditions and family history"""

    fh_condition = []
    fh_condition_coding = []
    condition = []
    condition_coding = []
    patient = None

    for row in connection.execute('select entity from patient where id = ? limit 1', (patient_id,)):
        patient = orjson.loads(row[0])
        break

    for row in connection.execute('select entity from family_history where patient_id = ? ', (patient_id,)):
        family_history = orjson.loads(row[0])
        for _ in family_history['condition']:
            if _ not in fh_condition:
                fh_condition.append(_)
        for _ in family_history['condition_coding']:
            if _ not in fh_condition_coding:
                fh_condition_coding.append(_)

    for row in connection.execute('select entity from condition where patient_id = ? ', (patient_id,)):
        condition_ = orjson.loads(row[0])
        if condition_['code'] not in condition:
            condition.append(condition_['code'])
            condition_coding.append(condition_['code_coding'])

    return {
        'patient': patient, 'condition': condition, 'condition_coding': condition_coding,
        'fh_condition': fh_condition, 'fh_condition_coding': fh_condition_coding
    }


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
        mapping = {
            # v 7
            # "mappings": {
            #     "properties": {
            #       "timestamp": {"type": "date"},
            #       "array": {"type": "keyword"},
            #     }
            # }

            # v 6
            "mappings": {
                "_doc": {
                    "properties": {
                        "timestamp": {"type": "date"},
                        "array": {"type": "keyword"},
                    }
                }
            }
        }
        elastic.indices.create(index=alias_index, body=mapping)
    except Exception as e:
        if 'resource_already_exists_exception' not in str(e):
            logger.warning(f"Could not create index. {index} {str(e)}")
            logger.warning(json.dumps(mapping))
            exit(1)
        #logger.warning("Continuing to load.")

    try:
        # elastic.create(alias_index, id=alias,  # v 7
        elastic.create(alias_index, id=alias, doc_type='_doc',  # v 6
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


@click.group('cli')
def cli():
    """Manage Gen3 Loads."""
    pass


@cli.group('load')
def load():
    """Load Gen3 databases."""
    pass


def write_flat_file(output_path, index, doc_type, limit, generator, schema):
    """Write the flat model to a file."""
    counter_ = 0

    pathlib.Path(output_path).mkdir(parents=True, exist_ok=True)

    with open(f"{output_path}/{doc_type}.ndjson", "wb") as fp:
        for dict_ in generator:
            fp.write(
                orjson.dumps(
                    {
                        'id': dict_['id'],
                        'object': dict_,
                        'name': doc_type,
                        'relations': []
                    }
                )
            )
            fp.write(b'\n')

            counter_ += 1
            if counter_ % 10000 == 0:
                logger.info(f"{counter_} records written")
        logger.info(f"{counter_} records written")


@cli.command('denormalize-patient')
@click.option('--input_path', required=True,
              default=None,
              show_default=True,
              help='Path to flattened json'
              )
def _denormalize_patient(input_path):
    """Gather Patient, FamilyHistory, Condition into sqlite db."""

    path = pathlib.Path(input_path)

    def _load_vertex(file_name):
        """Get the object and patient id"""
        if not (path / file_name).is_file():
            return
        for _ in read_ndjson(path / file_name):
            patient_id = None
            if len(_['relations']) == 1 and _['relations'][0]['dst_name'] == 'Patient':
                patient_id = _['relations'][0]['dst_id']
            _ = _['object']
            _['id'] = _['id']
            if patient_id:
                _['patient_id'] = patient_id
            yield _

    connection = sqlite3.connect('denormalized_patient.sqlite')
    with connection:
        connection.execute(f'DROP table IF EXISTS patient')
        connection.execute(f'DROP table IF EXISTS family_history')
        connection.execute(f'DROP table IF EXISTS condition')
        connection.execute(f'CREATE TABLE if not exists patient (id PRIMARY KEY, entity Text)')
        connection.execute(f'CREATE TABLE if not exists family_history (id PRIMARY KEY, patient_id Text, entity Text)')
        connection.execute(f'CREATE TABLE if not exists condition (id PRIMARY KEY, patient_id Text, entity Text)')
    with connection:
        connection.executemany(f'insert into patient values (?, ?)',
                               [(entity['id'], orjson.dumps(entity).decode(),) for entity in _load_vertex('Patient.ndjson')])
    with connection:
        connection.executemany(f'insert into family_history values (?, ?, ?)',
                               [(entity['id'], entity['patient_id'], orjson.dumps(entity).decode(),) for entity in _load_vertex('FamilyMemberHistory.ndjson')])
    with connection:
        connection.executemany(f'insert into condition values (?, ?, ?)',
                               [(entity['id'], entity['patient_id'], orjson.dumps(entity).decode(),) for entity in _load_vertex('Condition.ndjson')])
    with connection:
        connection.execute(f'CREATE INDEX if not exists condition_patient_id on condition(patient_id)')
        connection.execute(f'CREATE INDEX if not exists family_history_patient_id on condition(patient_id)')


@load.command('flat')
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
@click.option('--schema_path', required=True,
              default='generated-json-schema/aced.json',
              show_default=True,
              help='Path to gen3 schema json'
              )
@click.option('--output_path', required=False,
              default=None,
              show_default=True,
              help='Do not load elastic, write flat model to file instead'
              )
def load_flat(project_id, index, path, limit, elastic_url, schema_path, output_path):
    """Gen3 elastic search (guppy)."""
    # replaces tube_lite

    if limit:
        limit = int(limit)

    elastic = Elasticsearch([elastic_url], request_timeout=120)

    index = index.lower()

    # schema = requests.get(dictionary_url).json()
    schema = DataDictionary(local_file=schema_path).schema

    if index == 'patient':
        doc_type = 'patient'
        index = f"{DEFAULT_NAMESPACE}_{doc_type}_0"
        alias = 'patient'
        field_array = [k for k, v in schema['patient']['properties'].items() if 'array' in v.get('type', {})]

        if not output_path:
            # create the index and write data into it.
            write_bulk_http(elastic=elastic, index=index, doc_type=doc_type, limit=limit,
                            generator=patient_generator(project_id, path), schema=schema)

            setup_aliases(alias, doc_type, elastic, field_array, index)
        else:
            # write file path
            write_flat_file(output_path=output_path, index=index, doc_type=doc_type, limit=limit,
                            generator=patient_generator(project_id, path), schema=schema)

    if index == 'observation':
        doc_type = 'observation'
        index = f"{DEFAULT_NAMESPACE}_{doc_type}_0"
        alias = 'observation'
        field_array = [k for k, v in schema['observation']['properties'].items() if 'array' in v.get('type', {})]
        # field_array = ['data_format', 'data_type', '_file_id', 'medications', 'conditions']

        if not output_path:
            # create the index and write data into it.
            write_bulk_http(elastic=elastic, index=index, doc_type=doc_type, limit=limit,
                            generator=observation_generator(project_id, path), schema=schema)

            setup_aliases(alias, doc_type, elastic, field_array, index)
        else:
            # write file path
            write_flat_file(output_path=output_path, index=index, doc_type=doc_type, limit=limit,
                            generator=observation_generator(project_id, path), schema=schema)

    if index == 'file':
        doc_type = 'file'
        alias = 'file'
        index = f"{DEFAULT_NAMESPACE}_{doc_type}_0"

        field_array = [k for k, v in schema['document_reference']['properties'].items() if 'array' in v.get('type',
                                                                                                            {})]
        if not output_path:
            # create the index and write data into it.
            write_bulk_http(elastic=elastic, index=index, doc_type=doc_type, limit=limit,
                            generator=file_generator(project_id, path), schema=schema)

            setup_aliases(alias, doc_type, elastic, field_array, index)
        else:
            # write file path
            write_flat_file(output_path=output_path, index=index, doc_type=doc_type, limit=limit,
                            generator=file_generator(project_id, path), schema=schema)


def _connect_to_postgres(db_host, sheepdog_creds_path):
    """Use credential file, overloaded with passed db host to connect."""
    if pathlib.Path(sheepdog_creds_path).is_file():
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
    else:
        # read from env variables
        conn = psycopg2.connect('')
    return conn


def _init_dictionary(root_dir_=None, dictionary_url=None):
    """Initialize gen3 data dictionary from either directory or url"""
    d = DataDictionary(root_dir=root_dir_, url=dictionary_url)
    dictionary.init(d)
    # the gdcdatamodel expects dictionary initiated on load, so this can't be
    # imported on module level
    from gdcdatamodel import models as md

    return d, md


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


def chunk(arr_range, arr_size):
    """Iterate in chunks."""
    arr_range = iter(arr_range)
    return iter(lambda: tuple(islice(arr_range, arr_size)), ())


def load_vertices(files, connection, dependency_order, project_id, mapping):
    """Load files into database vertices."""
    logger.info(f"Number of files available for load: {len(files)}")
    for entity_name in dependency_order:
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
                for lines in chunk(f.readlines(), 1000):
                    buf = io.StringIO()
                    for line in lines:
                        record_count += 1
                        d_ = json.loads(line)
                        d_['object']['project_id'] = project_id
                        obj_str = json.dumps(d_['object'])
                        _csv = f"{d_['id']}\t{obj_str}\t{{}}\t{{}}\t{datetime.now()}".replace('\n', '\\n').replace("\\",
                                                                                                                   "\\\\")
                        _csv = _csv + '\n'
                        buf.write(_csv)
                    buf.seek(0)
                    # efficient way to write to postgres
                    cursor.copy_from(buf, data_table_name, sep='\t',
                                     columns=['node_id', '_props', 'acl', '_sysan', 'created'])
                    logger.info(f"wrote {record_count} records to {data_table_name} from {path}")
                    connection.commit()
        connection.commit()


def load_edges(files, connection, dependency_order, mapping, project_node_id):
    """Load files into database edges."""
    logger.info(f"Number of files available for load: {len(files)}")
    for entity_name in dependency_order:
        path = next(iter([fn for fn in files if str(fn).endswith(f"{entity_name}.ndjson")]), None)
        if not path:
            logger.warning(f"No file found for {entity_name} skipping")
            continue

        with connection.cursor() as cursor:
            print(path)
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
                            relations.append({"dst_id": project_node_id, "dst_name": "Project", "label": "project"})

                        if len(relations) == 0:
                            continue

                        record_count += 1
                        for relation in relations:

                            # entity_name_underscore = inflection.underscore(entity_name)
                            dst_name_camel = inflection.camelize(relation['dst_name'])

                            edge_table_mapping = next(
                                iter(
                                    [
                                        m for m in mapping
                                        if m['srcclass'] == entity_name and m['dstclass'] == relation['dst_name']
                                    ]
                                ),
                                None
                            )
                            if not edge_table_mapping and relation['dst_name'] in dependency_order:
                                msg = f"No mapping for src {entity_name} dst {relation['dst_name']}"
                                if msg not in LOGGED_ALREADY:
                                    logger.warning(msg)
                                    for m in mapping:
                                        logger.debug(m)
                                    LOGGED_ALREADY.append(msg)
                                continue
                            if not edge_table_mapping:
                                continue
                            table_name = edge_table_mapping['tablename']
                            # print(f"Mapping for src {entity_name} dst {relation['dst_name']} {table_name} {edge_table_mapping}")
                            buf = buffers[table_name]
                            # src_id | dst_id | acl | _sysan | _props | created |
                            buf.write(f"{d_['id']}|{relation['dst_id']}|{{}}|{{}}|{{}}|{datetime.now()}\n")
                    for table_name, buf in buffers.items():
                        buf.seek(0)
                        # efficient way to write to postgres
                        cursor.copy_from(buf, table_name, sep='|',
                                         columns=['src_id', 'dst_id', 'acl', '_sysan', '_props', 'created'])
                        logger.info(f"wrote {record_count} records to {table_name} from {path} {entity_name} {relation['dst_name']}")
        connection.commit()


@load.command('graph')
@click.option('--file_name_pattern',
              default='*.ndjson',
              show_default=True,
              help='File names to match.')
@click.option('--input_path',
              default='studies/',
              show_default=True,
              help='Path to transformed data.')
@click.option('--sheepdog_creds_path',
              default='../compose-services-training/Secrets/sheepdog_creds.json',
              show_default=True,
              help='Path to sheepdog credentials.')
@click.option('--program_name',
              default='aced',
              show_default=True,
              help='Existing program in Gen3.')
@click.option('--project_code',
              required=True,
              default='aced',
              show_default=True,
              help='Existing project in Gen3.')
@click.option('--db_host',
              default=None,
              show_default=True,
              help='Connect to db using this host')
@click.option('--config_path',
              default='gen3.config.yaml',
              show_default=True,
              help='Path to config file.')
@click.option('--dictionary_path',
              default=None,  # 'output/gen3',
              show_default=True,
              help='Path to data dictionary file.')
@click.option('--dictionary_url',
              default='https://aced-public.s3.us-west-2.amazonaws.com/aced.json',
              show_default=True,
              help='Data dictionary url.')
def data_load(input_path, file_name_pattern, sheepdog_creds_path, program_name, project_code, db_host, config_path,
              dictionary_path, dictionary_url):
    """Load transformed data to postgres."""

    config_path = pathlib.Path(config_path)
    assert config_path.is_file()
    with open(config_path) as fp:
        gen3_config = yaml.load(fp, SafeLoader)

    dependency_order = [c for c in gen3_config['dependency_order'] if not c.startswith('_')]
    dependency_order = [c for c in dependency_order if c not in ['Program', 'Project']]

    # check db connection
    conn = _connect_to_postgres(db_host, sheepdog_creds_path)
    assert conn
    logger.info("Connected to postgres")

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
    logger.info(f"Program and project exist: {project_id} {project_node_id}")

    # check files
    input_path = pathlib.Path(input_path) / project_code / "extractions"
    assert input_path.is_dir(), f"{input_path} should be a directory"
    files = [fn for fn in input_path.glob(file_name_pattern)]
    assert len(files) > 0, f"No files found at {input_path}/{file_name_pattern}"

    # check the mappings
    mappings = [mapping for mapping in _table_mappings(dictionary_path, dictionary_url)]

    # load the files
    logger.info("Loading vertices")
    load_vertices(files, conn, dependency_order, project_id, mappings)

    logger.info("Loading edges")
    load_edges(files, conn, dependency_order, mappings, project_node_id)
    logger.info("Done")


@load.command('discovery')
@click.option('--program', default="aced", show_default=True,
              help='Gen3 "program"')
@click.option('--gen3_credentials_file', default='Secrets/credentials.json', show_default=True,
              help='API credentials file downloaded from gen3 profile.')
def discovery(program, gen3_credentials_file):
    """Writes project information to discovery metadata-service"""
    endpoint = _extract_endpoint(gen3_credentials_file)
    auth = Gen3Auth(endpoint, refresh_file=gen3_credentials_file)
    discovery_client = Gen3Metadata(endpoint, auth)
    # TODO - read from some other, more dynamic source
    discovery_descriptions = """
Alcoholism~9300~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Alcoholism.  Data hosted by: aced-ohsu~aced-ohsu
Alzheimers~45306~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Alzheimer's, Familial Alzheimer's.  Data hosted by: aced-ucl~aced-ucl
Breast_Cancer~7105~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Malignant neoplasm of breast (disorder).  Data hosted by: aced-manchester~aced-manchester
Colon_Cancer~25355~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Malignant tumor of colon,  Polyp of colon.  Data hosted by: aced-stanford~aced-stanford
Diabetes~65051~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Diabetes.  Data hosted by: aced-ucl~aced-ucl
Lung_Cancer~25355~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Non-small cell carcinoma of lung,TNM stage 1,  Non-small cell lung cancer, Suspected lung cancer.  Data hosted by: aced-manchester~aced-manchester
Prostate_Cancer~35488~Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: Metastasis from malignant tumor of prostate, Neoplasm of prostate, arcinoma in situ of prostate.  Data hosted by: aced-stanford~aced-stanford""".split(
        '\n')

    for line in discovery_descriptions:
        if len(line) == 0:
            continue
        (name, _subjects_count, description, location,) = line.split('~')
        gen3_discovery = {'tags': [
            {"name": program, "category": "Program"},
            {"name": f"aced_{name}", "category": "Study Registration"},
            {"name": location, "category": "Study Location"},

        ], 'name': name, 'full_name': name, 'study_description': description}

        guid = f"aced_{name}"

        gen3_discovery['commons'] = "ACED"
        gen3_discovery['commons_name'] = "ACED Commons"
        gen3_discovery['commons_url'] = 'staging.aced-idp.org'
        gen3_discovery['__manifest'] = 0
        gen3_discovery['_research_subject_count'] = int(_subjects_count)
        gen3_discovery['_unique_id'] = guid
        gen3_discovery['study_id'] = guid
        discoverable_data = dict(_guid_type="discovery_metadata", gen3_discovery=gen3_discovery)
        discovery_client.create(guid, discoverable_data, aliases=None, overwrite=True)
        print(f"Added {name}")


def _extract_endpoint(gen3_credentials_file):
    """Get base url of jwt issuer claim."""
    with open(gen3_credentials_file) as input_stream:
        api_key = json.load(input_stream)['api_key']
        claims = jwt.decode(api_key, options={"verify_signature": False})
        assert 'iss' in claims
        return claims['iss'].replace('/user', '')


@cli.command('init')
@click.option('--gen3_credentials_file', default=f"{pathlib.Path.home()}/.gen3/credentials.json", show_default=True,
              help='API credentials file downloaded from gen3 profile.')
@click.option('--user_path',
              default='/creds/user.yaml',
              show_default=True,
              help='Path to gen3 user file.')
def init(gen3_credentials_file, user_path):
    """Initialize Gen3."""
    endpoint = _extract_endpoint(gen3_credentials_file)
    auth = Gen3Auth(endpoint, refresh_file=gen3_credentials_file)
    submission_client = Gen3Submission(endpoint, auth)
    actual_programs = [link.split('/')[-1] for link in submission_client.get_programs()['links']]
    user_path = pathlib.Path(user_path)

    assert user_path.is_file(), f"{user_path} is not a file?"
    with open(user_path) as fp:
        authz = yaml.load(fp, SafeLoader)['authz']

    expected_programs = next(iter([r for r in authz['resources'] if r['name'] == 'programs']), None)
    assert expected_programs, f"Could not find 'programs' resource in {user_path}"

    for ep in expected_programs['subresources']:
        program_name = ep['name']
        if program_name in actual_programs:
            print(f"Program:{program_name} already exists.")
        if ep['name'] not in actual_programs:
            response = submission_client.create_program(
                {'name': program_name, 'dbgap_accession_number': program_name, 'type': 'program'})
            assert response, 'could not parse response {}'.format(response)
            # assert 'code' in response, f'Unexpected response {response}'
            # assert response['code'] == 200, 'could not create {} program'.format(response)
            assert 'id' in response, 'could not create {} program'.format(response)
            assert program_name in response['name'], 'could not create {} program'.format(response)
            print(f"Program:{program_name} created.")

        actual_projects = submission_client.get_projects(program_name)
        actual_projects = [p_.split('/')[-1] for p_ in actual_projects['links']]
        expected_projects = next(iter([r for r in ep['subresources'] if r['name'] == 'projects']), None)
        for expected_project in expected_projects['subresources']:
            project_name = expected_project['name']
            if project_name in actual_projects:
                print(f"  Project:{project_name} already exists.")
                continue
            response = submission_client.create_project(program_name, {
                "type": "project",
                "code": project_name,
                "dbgap_accession_number": project_name,
                "name": project_name
            })
            assert response['code'] == 200, response
            print(f"  Project:{project_name} created.")

            # ctx.ensure_object(dict)
    # ctx.obj['submission_client'] = submission_client
    # ctx.obj['discovery_client'] = Gen3Metadata(endpoint, auth)
    # ctx.obj['endpoint'] = endpoint


if __name__ == '__main__':
    cli()
