import logging
import uuid
import click

import requests
import yaml
from pathlib import Path

from fhirclient.models.fhirreference import FHIRReference
from fhirclient.models.researchstudy import ResearchStudy
from fhirclient.models.researchsubject import ResearchSubject

ACED_NAMESPACE = uuid.uuid3(uuid.NAMESPACE_DNS, 'aced-ipd.org')

logging.basicConfig(format='%(asctime)s %(message)s',  encoding='utf-8', level=logging.INFO)
logger = logging.getLogger(__name__)

# study_manifests = {'Alcoholism': {'conditions': ['7200002'], 'expected_count': 45, 'bucket': 'aced-ohsu'},
#                    'Alzheimers': {'conditions': ['26929004', '230265002'], 'expected_count': 287, 'bucket': 'aced-ucl'},
#                    'BreastCancer': {'conditions': ['254837009'], 'expected_count': 57, 'bucket': 'aced-manchester'},
#                    'ColonCancer': {'conditions': ['363406005', '68496003'],
#                                    'expected_count': 312, 'bucket': 'aced-stanford'},
#                    'Diabetes': {'conditions': ['44054006'], 'expected_count': 180, 'bucket': 'aced-ucl'},
#                    'LungCancer': {'conditions': ['254637007', '424132000', '162573006'],
#                                   'expected_count': 98, 'bucket': 'aced-manchester'},
#                    'ProstateCancer': {'conditions': ['314994000', '126906006', '92691004'],
#                                       'expected_count': 378, 'bucket': 'aced-stanford'}
#                    }


headers = {
    "Content-Type": "application/fhir+json;charset=utf-8",
    # https://hapifhir.io/hapi-fhir/docs/server_jpa/performance.html#disable-upsert-existence-check
    # spelling error in code
    # https://github.com/hapifhir/hapi-fhir/blob/1b55f49a60f93b845b8e5d1d567584fb7ef032bb/hapi-fhir-jpaserver-model/src/main/java/ca/uhn/fhir/jpa/model/util/JpaConstants.java#L270
    # "X-Upsert-Extistence-Check": "disabled",
}


@click.group()
def cli():
    pass


# @cli.command()
# def manifest():
#     """Display manifest."""
#     print(yaml.dump(study_manifests))


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
@click.option('--manifest', default="coherent_studies.manifest.yaml", show_default=True,
              help='Study names, conditions, expected counts, etc.')
def test(url, manifest):
    """Check study manifest's patients with conditions counts."""
    study_manifests = yaml.load(open(manifest), yaml.SafeLoader)
    for name, values in study_manifests.items():
        response = requests.get(f"{url}/Condition?code={','.join(values['conditions'])}&_summary=count")
        assert response.json()['total'] == values['expected_count'],  (name, response.json())

    print('Condition counts OK.')


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
@click.option('--manifest', default="coherent_studies.manifest.yaml", show_default=True,
              help='Study names, conditions, expected counts, etc.')
@click.option('--study', default=None, show_default=True,
              help='Optional. If set, only extract this study')
def create(url, manifest, study):
    """Ensure research studies exist."""
    response = requests.get(f"{url}/ResearchStudy?_elements=id,title")
    study_bundle = response.json()
    total_research_study_count = study_bundle['total']
    if total_research_study_count > 0:
        print("Studies already exist.")
        print(study_bundle)
        # return
    study_manifests = yaml.load(open(manifest), yaml.SafeLoader)
    study_param = study
    for name, values in study_manifests.items():
        print(name)
        if study_param and name != study_param:
            continue
        if len(values['conditions']) == 0:
            print(f"No conditions for {name} skipping")
            continue
        print(f"Building {name}")

        response = requests.get(
            f"{url}/Condition?code={','.join(values['conditions'])}&_elements=subject&_count=1000")
        response.raise_for_status()
        response = response.json()
        assert response['total'] < 1000, f"TODO - write paging {name} total {response['total']}"  # TODO
        patients = [entry['resource']['subject']['reference'] for entry in response['entry']]
        assert len(patients) == values['expected_count']
        study = ResearchStudy()
        study.title = name
        study.id = str(uuid.uuid5(ACED_NAMESPACE, study.title))
        study.description = f"Patients from 'Coherent Data Set' https://www.mdpi.com/2079-9292/11/8/1199/htm that were diagnosed with condition(s) of: {name}.  Data hosted by: {values['bucket']}"
        study.status = 'active'
        response = requests.put(f"{url}/ResearchStudy/{study.id}", json=study.as_json(), headers=headers)
        if response.status_code > 201:
            print(response.json())
        response.raise_for_status()
        for patient in set(patients):
            research_subject = ResearchSubject()
            research_subject.id = str(uuid.uuid5(ACED_NAMESPACE, study.id + patient))
            research_subject.individual = FHIRReference({'reference': patient})
            research_subject.study = FHIRReference({'reference': study.relativePath()})
            research_subject.status = 'on-study'
            response = requests.put(f"{url}/ResearchSubject/{research_subject.id}", json=research_subject.as_json(), headers=headers)
            if response.status_code > 201:
                print(response.json())
            response.raise_for_status()


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
def ls(url):
    """Print details about studies."""
    response = requests.get(f"{url}/ResearchStudy?_elements=id,title")
    study_bundle = response.json()
    total_research_study_count = study_bundle['total']

    print(f"There are {total_research_study_count} studies")
    if 'entry' in study_bundle:
        for study_entry in study_bundle['entry']:
            id_ = study_entry['resource']['id']
            title = study_entry['resource']['title']
            response = requests.get(f"{url}/ResearchSubject?study={id_}&_summary=count")
            response.raise_for_status()
            subjects_bundle = response.json()
            total_subjects = subjects_bundle['total']
            print(f"  {title} has {total_subjects} subjects")


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
@click.option('--destination_directory', default='./studies', show_default=True,
              help='directory to store bundles')
@click.option('--limit', default=None, show_default=True,
              help='Maximum number of Patients per study to export')
@click.option('--title', default=None, show_default=True,
              help='filter by this single study title')
def extract(url, destination_directory, limit, title):
    """Extract data from ALL ResearchStudy."""

    # all the details
    filter_url = f"{url}/ResearchStudy?_elements=id,title"
    if title:
        filter_url = f"{filter_url}&title={title}"
    response = requests.get(filter_url)
    study_bundle = response.json()
    total_research_study_count = study_bundle['total']
    if limit:
        limit = int(limit)

    print(f"There are {total_research_study_count} studies")
    for study_entry in study_bundle['entry']:
        id_ = study_entry['resource']['id']
        title = study_entry['resource']['title']
        Path(f"{destination_directory}/{title}").mkdir(parents=True, exist_ok=True)

        # study and subjects
        response = requests.get(f"{url}/ResearchStudy?_id={id_}&_revinclude=ResearchSubject:study")
        studies_bundle = str(response.text)
        file_name = f"{destination_directory}/{title}/study.bundle.json"
        with open(file_name, "w") as f:
            f.write(studies_bundle)
        print(f"    wrote {file_name}")

        # all the details
        response = requests.get(f"{url}/ResearchSubject?study={id_}&_count=1000")
        response.raise_for_status()
        subjects_bundle = response.json()
        total_subjects = subjects_bundle['total']
        print(f"{title} has {total_subjects} subjects")
        assert total_subjects < 1000, f"TODO - write paging {title} total {total_subjects}"  # TODO
        count = 0
        for subject_entry in subjects_bundle['entry']:
            subject = subject_entry['resource']
            individual = subject['individual']
            id_ = individual['reference'].split('/')[-1]
            everything = f"{url}/Patient?_id={id_}&_revinclude=Specimen:subject&_revinclude=DocumentReference:subject&_revinclude=Encounter:subject&_revinclude=Observation:subject&_revinclude=Condition:subject&_revinclude=Task:patient&_revinclude=MedicationAdministration:subject"
            response = requests.get(everything)
            response.raise_for_status()
            patient_bundle = str(response.text)
            file_name = f"{destination_directory}/{title}/{id_}.bundle.json"
            with open(file_name, "w") as f:
                f.write(patient_bundle)
            print(f"    wrote {file_name} " + str(patient_bundle.count('"id"')) + " resources")
            count += 1
            if limit and count == limit:
                print(f"    hit limit {limit}")
                break


@cli.command()
@click.option('--url', default="http://localhost:8090/fhir", show_default=True,
              help='url to HAPI FHIR server')
def rm(url):
    """Removes ALL ResearchStudy, ResearchSubject (leaves Patient, etc intact)."""
    response = requests.get(f"{url}/ResearchStudy?_elements=id,title")
    study_bundle = response.json()
    total_research_study_count = study_bundle['total']
    print(f"There are {total_research_study_count} studies")
    if total_research_study_count == 0:
        return
    for study_entry in study_bundle['entry']:
        id_ = study_entry['resource']['id']
        title = study_entry['resource']['title']
        response = requests.get(f"{url}/ResearchSubject?study={id_}&_count=1000")
        response.raise_for_status()
        subjects_bundle = response.json()
        total_subjects = subjects_bundle['total']
        print(f"{title} has {total_subjects} subjects")
        assert total_subjects < 1000, f"TODO - write paging {title} total {total_subjects}"  # TODO
        for subject_entry in subjects_bundle['entry']:
            requests.delete(f"{url}/ResearchSubject/{subject_entry['resource']['id']}")
            response.raise_for_status()
        requests.delete(f"{url}/ResearchStudy/{id_}")
        response.raise_for_status()
        print(f"  deleted {title}")


if __name__ == '__main__':
    cli()
