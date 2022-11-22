import json
import logging
import uuid
from typing import Dict, Iterator

import click
import pandas as pd

import requests
import yaml
from pathlib import Path

from fhirclient.models.codeableconcept import CodeableConcept
from fhirclient.models.coding import Coding
from fhirclient.models.fhirreference import FHIRReference
from fhirclient.models.observation import Observation, ObservationComponent
from fhirclient.models.researchstudy import ResearchStudy
from fhirclient.models.researchsubject import ResearchSubject

ACED_NAMESPACE = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced-ipd.org')

logging.basicConfig(format='%(asctime)s %(message)s',  encoding='utf-8', level=logging.INFO)
logger = logging.getLogger(__name__)

headers = {
    "Content-Type": "application/fhir+json;charset=utf-8",
}


@click.group()
def cli():
    pass


def _parse_assertation(assertation_sentence: str) -> Dict[str, str]:
    """Parse sentence."""
    gene = assertation_sentence.split('The ')[1].split(' gene')[0]

    assertation_parts = assertation_sentence.split('of ')
    significance = assertation_parts[-1].split("'")[1]

    assertation_parts = assertation_sentence.split("index ")
    variant_id = assertation_parts[-1].split(" ")[0]
    snp_id = variant_id
    snp_capture = None
    # FS Follistatin? FeatureSelection ? Frame Shift ?
    if '_' in variant_id:
        snp_id, snp_capture = variant_id.split('_')

    assertation_parts = assertation_sentence.split("of: ")
    conditions = assertation_parts[-1].replace(' and ', ',').replace('.', '').split(',')
    risk = assertation_parts[0].split(' an ')[-1].strip()

    return {'gene': gene, 'significance': significance, 'snp_id': snp_id, 'snp_capture': snp_capture, 'risk': risk, 'conditions': conditions}


def _get_observations(url) -> Iterator[Dict]:
    """Grab all the genomic observations."""
    next_observations = f"{url}/Observation?code=69548-6&_count=1000"  # &_elements=code.coding.display
    while next_observations:
        response = requests.get(next_observations)
        observation_bundle = response.json()
        for observation in observation_bundle['entry']:
            yield observation
        next_observations = next(iter([l_['url'] for l_ in observation_bundle['link'] if l_['relation'] == 'next']),
                                 None)


def _get_implications(url) -> Iterator[Dict]:
    """Grab all the genomic observation sentences and parse them."""
    for observation in _get_observations(url):
        yield _parse_assertation(observation['resource']['code']['coding'][0]['display'])


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
def ls(url):
    """Print details about genomic observations."""
    for observation in _get_observations(url):
        print(json.dumps(observation, separators=(',', ':')))


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
def parse(url):
    """Parse the assertations."""
    for implication in _get_implications(url):
        print(json.dumps(implication, separators=(',', ':')))


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
@click.option('--export', default=False, show_default=True, is_flag=True,
              help='Write observations to observation.tsv')
@click.option('--plot', default=False, show_default=True, is_flag=True,
              help='Display simple bar graph figures')
def analyze(url, export, plot):
    """Print analysis of assertations."""
    df = pd.json_normalize(_get_implications(url))
    if export:
        file_name = "observation.tsv"
        df.to_csv(file_name, sep="\t")
        print(f"Wrote {file_name}")
        return
    if plot:
        for column in ['gene', 'significance', 'snp_id', 'snp_capture', 'risk']:
            df[column].value_counts().plot(kind='barh', title=column).get_figure().savefig(f"{column}.png")
            print(f"Wrote: {column}.png")
        return
    print(df)


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
@click.option('--show', default=False, show_default=True, is_flag=True,
              help='Show the transformation.')
@click.option('--load', default=False, show_default=True, is_flag=True,
              help='Write transformed data back to database.')
def transform(url, show, load):
    """Transform to a GenomicInterpretation."""

    for observation_dict in _get_observations(url):
        print('# BEFORE')
        print(yaml.dump(observation_dict['resource']))

        # fix missing data
        if 'status' not in observation_dict['resource'] or not observation_dict['resource']['status']:
            observation_dict['resource']['status'] = 'preliminary'

        # load into a python object
        observation = Observation(observation_dict['resource'], strict=False)

        # TODO - change profile ? http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/implication
        # parse the assertation
        assertation_sentence = observation.code.coding[0].display
        implication = _parse_assertation(assertation_sentence)

        # cast the observation into a full GenomicInterpretation
        observation.category = []

        # set category
        observation.category.append(
            CodeableConcept(
                {
                    'coding': [
                        {
                            'system': "https://loinc.org",
                            'code': '55233-1',
                            'display': 'Genetic analysis master panel'
                        },
                        {
                            'system': "http://terminology.hl7.org/CodeSystem/observation-category",
                            'code': 'laboratory',
                            'display': 'laboratory'
                        },
                    ]
                }
            )
        )

        observation.code = CodeableConcept({
            'coding': [{
                "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
                "code": "diagnostic-implication"
            }]
        })

        # set generic summary view
        observation.valueString = assertation_sentence
        observation.valueCodeableConcept = CodeableConcept(
            {
                "coding": [
                    {
                        "system": "http://www.genenames.org/geneId",
                        "code": implication['gene'],
                        "display": implication['gene']
                    }
                ]
            }
        )

        # set detailed view
        if not observation.component:
            observation.component = []

        # geneId
        observation.component.append(ObservationComponent({
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "48018-6",
                        "display": "Gene studied ID"
                    }
                ]
            },
            "valueCodeableConcept": {
                "coding": [
                    {
                        "system": "http://www.genenames.org/geneId",
                        "code": implication['gene'],
                        "display": implication['gene']
                    }
                ]
            }
        }))

        # snp_id
        observation.component.append(ObservationComponent({
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "48013-7",
                        "display": "Genomic reference sequence ID"
                    }
                ]
            },
            "valueCodeableConcept": {
                "coding": [
                    {
                        "system": "https://www.ncbi.nlm.nih.gov/snp/",
                        "code": implication['snp_id'],
                        "display": implication['snp_id']
                    }
                ]
            }
        }))

        # conclusion
        observation.component.append(ObservationComponent({
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
                        "code": "conclusion-string",
                        "display": "conclusion-string"
                    }
                ]
            },
            "valueString": assertation_sentence
        }))

        # evidence-level
        observation.component.append(ObservationComponent({
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
                        "code": "evidence-level",
                        "display": "evidence-level"
                    }
                ]
            },

            # TODO - translate this vocabulary
            "valueCodeableConcept": {
                "coding": [
                    {
                        "system": "http://loinc.org/LL5356-2/",
                        "code": f"TODO lookup code for: {implication['significance']}",
                        "display": implication['significance']
                    }
                ]
            }
        }))

        # predicted-phenotype
        observation.component.append(ObservationComponent({
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
                        "code": "predicted-phenotype",
                        "display": "predicted-phenotype"
                    }
                ]
            },

            # TODO - translate this vocabulary
            "valueCodeableConcept": {
                "coding": [
                    {
                        "system": "http://snomed.info/sct",
                        "code": f"TODO - lookup snomed code for {condition}",
                        "display": condition
                    }
                    for condition in implication['conditions']
                ]
            }
        }))

        # observation-interpretation
        observation.component.append(ObservationComponent({
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
                        "code": "observation-interpretation",
                        "display": "observation-interpretation"
                    }
                ]
            },

            # TODO - translate this vocabulary
            "valueCodeableConcept": {
                "coding": [
                    # {
                    #     "system": "http://hl7.org/fhir/ValueSet/observation-interpretation",
                    #     "code": f"TODO - lookup FHIR code for {implication['risk']}",
                    #     "display": implication['risk']
                    # }
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/risk-probability",
                        "code": 'moderate',
                        "display": 'The specified outcome has a reasonable likelihood of occurrence.'
                    }

                ]
            }
        }))

        print('# AFTER')
        print(yaml.dump(observation.as_json()))
        exit(1)


if __name__ == '__main__':
    cli()
