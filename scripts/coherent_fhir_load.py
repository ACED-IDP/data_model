import asyncio
import logging
import time
from pathlib import Path
from typing import Iterator

import aiohttp
import click
from fhirclient.models.bundle import BundleEntryRequest

logging.basicConfig(format='%(asctime)s %(message)s',  encoding='utf-8', level=logging.INFO)
logger = logging.getLogger(__name__)


def bundle_entry_request_as_json(self, strict=True):
    """Preserve the IDs. Patched as_json to PUT not post see https://github.com/hapifhir/hapi-fhir/issues/333."""
    if self.method == 'POST' and self._owner.resource.id:
        self.method = 'PUT'
        self.url += f"/{self._owner.resource.id}"
    return {'method': self.method, 'url': self.url}


BundleEntryRequest.as_json = bundle_entry_request_as_json

headers = {
    "Content-Type": "application/fhir+json;charset=utf-8",
    # https://hapifhir.io/hapi-fhir/docs/server_jpa/performance.html#disable-upsert-existence-check
    "X-Upsert-Extistence-Check": "disabled",
}


async def load(path, url):
    """Read a bundle, load it to server."""
    print(path)
    session = None
    try:
        tic = time.perf_counter()
        with open(path, 'r') as data:
            bundle = data.read()
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url=url, data=bundle) as response:
                if response.status != 200:
                    logger.error(await response.json())
                response.raise_for_status()
        session = None
        toc = time.perf_counter()
        logger.info(f"POST {path} {toc - tic:0.4f} seconds")
        return True
    finally:
        if session:
            await session.close()


def _chunker(seq: Iterator, size: int) -> Iterator:
    """Iterate over a list in chunks.

    Args:
        seq: an iterable
        size: desired chunk size

    Returns:
        an iterator that returns lists of size or less
    """
    return (seq[pos:pos + size] for pos in range(0, len(seq), size))


async def load_all(coherent_path, url, chunk_size):
    """Load all the bundles"""

    # load dependencies in order, resources that must exist prior
    # TODO - does Medication, etc. belong here too?
    paths = [f'{coherent_path}/fhir/organizations.json', f'{coherent_path}/fhir/practitioners.json']
    print(paths)
    ok = False
    for chunk in _chunker(paths, 1):
        tasks = []
        for path in chunk:
            task = asyncio.create_task(load(path=path, url=url))
            tasks.append(task)

        ok = all([
            await ok_
            for ok_ in asyncio.as_completed(tasks)
        ])

    assert ok, "Did not load dependencies"

    # get all the patients
    paths = sorted([p for p in Path(f'{coherent_path}/fhir/').glob('*.json') if
                    'organizations' not in str(p) and 'practitioners' not in str(p)])

    # doing this as a maximum of 3 seems to work when combined with nice -10 on a laptop
    limit = None
    count = 0
    for chunk in _chunker(paths, chunk_size):
        tasks = []

        for path in chunk:
            task = asyncio.create_task(load(path=path, url=url))
            tasks.append(task)
            count += 1
            if limit and count == limit:
                break

        ok = all([
            await ok_
            for ok_ in asyncio.as_completed(tasks)
        ])

        if limit and count == limit:
            break

    assert ok, "Did not load bundles"


@click.command()
@click.option('--coherent_path', default='output', show_default=True,
              help='Unzipped directory: see http://hdx.mitre.org/downloads/coherent-11-17-2022.zip')
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
@click.option('--chunk_size', default=5, show_default=True,
              help='Number of simultaneous loaders')
def main(coherent_path, url, chunk_size):
    """Load coherent study into a FHIR server"""
    asyncio.run(load_all(coherent_path, url, chunk_size))
    logger.info('done')


if __name__ == '__main__':
    main()

"""
Ad hoc testing These queries should work
http://localhost:8090/fhir/Patient?_id=6f60d183-2b8d-8c3c-77d0-2b684653651e&_revinclude=Specimen:subject&_revinclude=DocumentReference:subject&_revinclude=Encounter:subject&_revinclude=Observation:subject&_revinclude=MedicationRequest:subject&_revinclude=Condition:subject

http://localhost:8090/fhir/Condition?code=26929004,26929004&_summary=count
 
"""