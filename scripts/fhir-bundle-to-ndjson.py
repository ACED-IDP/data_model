# reads all bundles in a directory and converts them to ndjson

import os
import sys
import json
from typing import Any

import yaml
import logging
import click
import pathlib
import subprocess

# setup logging
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
# setup default logging format
logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

EMITTERS = {}

def emitter(resource:dict, output_path:pathlib.Path) -> Any:
    """Maintain has of open files."""
    file_path = output_path / f"{resource['resourceType']}.ndjson"

    if str(file_path) not in EMITTERS:
        EMITTERS[str(file_path)] = open(file_path, 'w')

    return EMITTERS[str(file_path)]

def close_emitters():
    """Close all open files."""
    for file in EMITTERS.values():
        file.close()


def resource_generator(file_path):
    """Yield resources from a FHIR bundle."""
    if not file_path.exists():
        logger.warning(f"{file_path} does not exist")
        raise StopIteration
    try:
        with file_path.open() as _:
            data = json.load(_)
            logging.info(f"loaded {file_path} {data}")
            assert 'resourceType' in data, f"{file_path} is not a FHIR bundle"
            assert data['resourceType'] == 'Bundle', f"{file_path} is not a FHIR bundle"
            for entry in data['entry']:
                yield entry['resource']
    except json.decoder.JSONDecodeError:
        logger.warning(f"{file_path} is not valid json")
    except AssertionError as e:
        logger.warning(e)

@click.command()
@click.argument('input_path', default='.', required=True)
@click.argument('output_path', default='output/', required=True)
def _main(input_path, output_path):
    """Convert FHIR bundles to ndjson."""

    output_path = pathlib.Path(output_path)
    if not output_path.exists():
        output_path.mkdir(parents=True, exist_ok=True)

    for file_path in pathlib.Path(input_path).glob('*.json'):
        logger.info(f"converting {file_path} to ndjson")
        for resource in resource_generator(file_path):
            _ = emitter(resource, output_path)
            json.dump(resource, _)
            _.write('\n')
    close_emitters()
    logger.info(f"files written to {output_path}")

if __name__ == "__main__":
   _main()