import json
import logging
import click

from fhirclient.models.bundle import Bundle

logging.basicConfig(level=logging.INFO, format='%(process)d - %(levelname)s - %(message)s')


@click.command()
@click.option('--coherent_path', default='coherent-11-17-2022', show_default=True,
              help='Unzipped directory: see http://hdx.mitre.org/downloads/coherent-11-17-2022.zip')
def fix_organization_bundle(coherent_path):
    """Fixes Location resources in coherent study."""
    with open(f'{coherent_path}/fhir/organizations.json') as f:
        bundle = json.load(f)
    for entry in bundle['entry']:
        resource = entry['resource']
        if resource['resourceType'] == 'Location':
            # location's address is a scalar, not an array
            # see http://hl7.org/fhir/r4/location-definitions.html#Location.address
            resource['address'] = resource['address'][0]
        if 'position' in resource:
            # position lat long are numeric
            # see https://hl7.org/fhir/r4/location-definitions.html#Location.position
            resource['position']['longitude'] = float(resource['position']['longitude'])
            resource['position']['latitude'] = float(resource['position']['latitude'])
    # write it into a python class that will validate it.
    bundle = Bundle(bundle)
    with open(f'{coherent_path}/fhir/organizations.json', 'w') as f:
        json.dump(bundle.as_json(), f)


if __name__ == '__main__':
    fix_organization_bundle()
