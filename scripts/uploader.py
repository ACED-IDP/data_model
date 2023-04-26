#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import urllib
import uuid
from itertools import islice

import click
import jwt
import requests
from gen3.auth import Gen3Auth
from gen3.file import Gen3File
from gen3.index import Gen3Index

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

LOGGED_ALREADY = set({})

ACED_CODEABLE_CONCEPT = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced.ipd/CodeableConcept')
ACED_NAMESPACE = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced-ipd.org')


def chunk(arr_range, arr_size):
    """Iterate in chunks."""
    arr_range = iter(arr_range)
    return iter(lambda: tuple(islice(arr_range, arr_size)), ())


@click.group()
def cli():
    """Manage data model schema and transforms."""
    pass


def extract_endpoint(gen3_credentials_file):
    """Get base url of jwt issuer claim."""
    with open(gen3_credentials_file) as input_stream:
        api_key = json.load(input_stream)['api_key']
        claims = jwt.decode(api_key, options={"verify_signature": False})
        assert 'iss' in claims
        return claims['iss'].replace('/user', '')


async def upload_and_decorate_document_reference(document_reference, bucket_name,
                                                 file_client, index_client, program,
                                                 project):
    """Write to indexd."""

    if 'content_url' not in document_reference:
        logger.warning('content_url not found')
        return 
    md5sum = document_reference["md5sum"]
    object_name = document_reference['file_name'].lstrip('./')

    hashes = {'md5': md5sum}
    assert 'id' in document_reference, document_reference
    guid = document_reference['id']
    metadata = {
        **{
            'datanode_type': 'DocumentReference',
            'datanode_submitter_id': document_reference.get('submitter_id', None),
            'datanode_object_id': guid
        },
        **hashes}

    # SYNC
    create_record_response = index_client.create_record(
        did=document_reference["id"],
        hashes=hashes,
        size=document_reference["file_size"],
        authz=[f'/programs/{program}/projects/{project}'],
        file_name=document_reference['file_name'],
        metadata=metadata,
        urls=[f"s3://{bucket_name}/{guid}/{object_name}"]
    )
    # create a record in gen3 using document_reference's id as guid, get a signed url
    # SYNC
    document = file_client.upload_file_to_guid(guid=document_reference['id'], file_name=object_name, bucket=bucket_name)
    assert 'url' in document, document
    signed_url = urllib.parse.unquote(document['url'])
    guid = document_reference['id']

    with open(document_reference['file_name'], 'rb') as data_f:
        # When you use this header, Amazon S3 checks the object against the provided MD5 value and,
        # if they do not match, returns an error.
        content_md5 = base64.b64encode(bytes.fromhex(md5sum))
        headers = {'Content-MD5': content_md5}
        # attach our metadata to s3 object
        for key, value in metadata.items():
            headers[f"x-amz-meta-{key}"] = value
        # SYNC
        r = requests.put(signed_url, data=data_f, headers=headers)
        assert r.status_code == 200, (signed_url, r.text)
        logger.info(
            f"Successfully uploaded resource {document_reference['id']} file_name \"{document_reference['file_name']}\" to {bucket_name} {guid}")
        document_reference['object_id'] = document_reference['id']
        return document_reference


async def upload_and_decorate_document_references(lines, bucket_name, program,
                                                  project, file_client, index_client):
    records = []

    for line in lines:
        record = json.loads(line)
        document_reference = record['object']
        document_reference = await upload_and_decorate_document_reference(
            document_reference=document_reference,
            file_client=file_client,
            bucket_name=bucket_name,
            index_client=index_client,
            program=program,
            project=project,
        )
        record['object'] = document_reference
        records.append(record)
    return records


@cli.command(name='upload-files')
@click.option('--bucket_name', default='aced-default', show_default=True,
              help='Destination bucket name')
@click.option('--document_reference_path', required=True, default=None, show_default=True,
              help='Path to DocumentReference.ndjson')
@click.option('--program', required=True, show_default=True,
              help='Gen3 program')
@click.option('--project', required=True, show_default=True,
              help='Gen3 project')
@click.option('--credentials_file', default='credentials.json', show_default=True,
              help='API credentials file downloaded from gen3 profile.')
@click.pass_context
def upload_document_reference(ctx, bucket_name, document_reference_path, program, project, credentials_file):
    """Upload data file found in DocumentReference.ndjson"""
    endpoint = extract_endpoint(credentials_file)
    logger.info(endpoint)
    logger.debug(f"Read {credentials_file} endpoint {endpoint}")
    auth = Gen3Auth(endpoint, refresh_file=credentials_file)
    file_client = Gen3File(endpoint, auth)
    index_client = Gen3Index(endpoint, auth)

    with open(document_reference_path + '.tmp', "w") as output_f:
        with open(document_reference_path) as input_f:
            for lines in chunk(input_f.readlines(), 10):
                loop = asyncio.get_event_loop()
                records = loop.run_until_complete(
                    upload_and_decorate_document_references(
                        lines=lines,
                        bucket_name=bucket_name,
                        program=program,
                        project=project,
                        file_client=file_client,
                        index_client=index_client
                    )
                )
                for record in records:
                    json.dump(record, output_f, separators=(',', ':'))
                    output_f.write('\n')
                output_f.flush()


if __name__ == '__main__':
    cli()
