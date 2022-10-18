import csv
import json
from typing import Mapping, Sequence, Set, List

import click
import os
from pathlib import Path
import time
import multiprocessing
import logging
from fhirclient.models.bundle import Bundle, BundleEntry
from fhirclient.models.documentreference import DocumentReference
from fhirclient.models.researchstudy import ResearchStudy
from fhirclient.models.researchsubject import ResearchSubject
from fhirclient.models.specimen import Specimen
from fhirclient.models.task import Task, TaskInput, TaskOutput
from fhirclient.models.fhirreference import FHIRReference
from fhirclient.models.narrative import Narrative
from fhirclient.models.organization import Organization
from fhirclient.models.location import Location
from fhirclient.models.practitioner import Practitioner
from pydantic import BaseModel
from fhirclient.models.extension import Extension

import base64
import uuid
from itertools import repeat


logging.basicConfig(level=logging.INFO, format='%(process)d - %(levelname)s - %(message)s')


def create_global_resources(coherent_path) -> list:
    """Reads CSV files for Organizations, Practitioners etc. used across bundles.

    Required since Mitre did not provide FHIR resources for same.
    """
    global_resources = []

    with open(f'{coherent_path}/output/csv/providers.csv') as csvfile:
        reader = csv.DictReader(csvfile)
        for organization_ in reader:
            practitioner_ = {}
            for k in ['Id', 'ORGANIZATION', 'NAME', 'GENDER', 'SPECIALITY']:
                practitioner_[k.lower()] = organization_.pop(k)
            practitioner_['gender'] = 'female' if practitioner_['gender'] == 'F' else 'male'
            practitioner_['name'] = [{'text': practitioner_.pop('name')}]
            speciality = practitioner_.pop('speciality')
            practitioner_['qualification'] = [
                {
                    "code": {
                      "coding": [
                          {
                              "system": "https://github.com/synthetichealth/synthea",
                              "code": speciality,
                              "display": speciality
                          }
                      ],
                      "text": speciality
                    },
                    "issuer": {
                        "reference": f"Organization/{practitioner_.pop('organization')}"
                    }

                }
            ]
            practitioner_['identifier'] = [
                {
                    'system': 'https://github.com/synthetichealth/synthea',
                    'value': practitioner_['id']
                },
                {
                    'system': 'name',
                    'value': ''.join(filter(str.isalnum, practitioner_['name'][0]['text']))
                }

            ]
            global_resources.append(Practitioner(practitioner_))

    with open(f'{coherent_path}/output/csv/organizations.csv') as csvfile:
        reader = csv.DictReader(csvfile)
        for organization_ in reader:
            for k in ['Id', 'NAME', 'ADDRESS', 'CITY', 'STATE', 'ZIP', 'LAT', 'LON', 'PHONE', 'REVENUE', 'UTILIZATION']:
                organization_[k.lower()] = organization_.pop(k)
            telecom = {'value': organization_.pop('phone'), 'system': 'phone'}
            organization_['telecom'] = [telecom]
            location = {}
            address = {}
            for k in ['address', 'city', 'state', 'zip', 'lat', 'lon']:
                address[k] = organization_.pop(k)
            address['line'] = [address.pop('address')]
            address['postalCode'] = address.pop('zip')
            location['address'] = address
            location['position'] = {
                "latitude": float(address.pop('lat')),
                "longitude": float(address.pop('lon'))
            }
            location['id'] = str(uuid.uuid5(uuid.UUID(organization_['id']), str(location['position'])))
            # discard unmapped fields
            for k in ["revenue", "utilization"]:
                organization_.pop(k)
            # add identifier
            organization_['identifier'] = [
                {
                    'system': 'https://github.com/synthetichealth/synthea',
                    'value': organization_['id']
                },
                {
                    'system': 'name',
                    'value': ''.join(filter(str.isalnum, organization_['name']))
                }
            ]

            organization = Organization(organization_)
            global_resources.append(organization)
            location['identifier'] = [
                {
                    'system': 'name',
                    'value': ''.join(filter(str.isalnum, organization_['name']))
                }
            ]
            location['managingOrganization'] = {'reference': f"Organization/{organization.id}"}
            global_resources.append(Location(location))

    return global_resources


def _normalize_references(bundle, global_resources, file_path) -> Bundle:
    """Ensure that all reference identifiers transformed formed to ResourceType/id"""
    logged_already = []
    g = {
        f'urn:uuid:{e.resource.id}': f"{e.resource.resource_type}/{e.resource.id}" for e in bundle.entry
    }

    for e in bundle.entry:
        if hasattr(e.resource, 'identifier') and e.resource.identifier:
            for identifier in e.resource.identifier:
                g[f"{e.resource.resource_type}?identifier={identifier.system}|{identifier.value}"] = f"{e.resource.resource_type}/{e.resource.id}"
        if hasattr(e.resource, 'contained') and e.resource.contained:
            for contained in e.resource.contained:
                # TODO - ensure that contained resources make it to destination system
                g[f"#{contained.id}"] = f"#{contained.id}"

    is_global = []
    add_to_research_study_bundle = []
    for resource in global_resources:
        if hasattr(resource, 'identifier') and resource.identifier:
            for identifier in resource.identifier:
                g[f"{resource.resource_type}?identifier={identifier.system}|{identifier.value}"] = f"{resource.resource_type}/{resource.id}"
                is_global.append(f"{resource.resource_type}/{resource.id}")

    # see https://gist.github.com/sente/1480558
    string_types = (str, unicode) if str is bytes else (str, bytes)
    iteritems = lambda mapping: getattr(mapping, 'iteritems', mapping.items)()

    def __iter__(obj_):
        for attr, value in obj_.__dict__.items():
            if not attr.startswith('_'):
                yield attr, value

    def obj_walk(obj, path=(), memo=None):
        """Traverse the obj, handle references."""
        if memo is None:
            memo = set()
        iterator = None
        if isinstance(obj, Mapping):
            iterator = iteritems
        elif 'fhirclient' in obj.__class__.__module__:
            iterator = __iter__
        elif isinstance(obj, (Sequence, Set)) and not isinstance(obj, string_types):
            iterator = enumerate

        if 'fhirclient.models.fhirreference' == obj.__class__.__module__:
            iterator = None
            if obj.reference is None:
                pass
            elif obj.reference not in g:
                found = True
                if '/' in obj.reference and '?' not in obj.reference:
                    pass
                elif '?' in obj.reference:
                    found = False
                    name_reference = obj.reference.split('=')[0]
                    stripped_name = ''.join(filter(str.isalnum, obj.display)).replace("Dr", '')
                    name_reference += f'=name|{stripped_name}'
                    if name_reference in g:
                        # logging.info(f"{obj.reference} {name_reference} found in G {g[name_reference]}")
                        obj.reference = g[name_reference]
                        found = True
                if not found:
                    if f"{obj.reference} {obj.display}" not in logged_already:
                        logging.warning(f"{obj.reference} {obj.display} not found in bundle {path} {file_path}")
                        logged_already.append(f"{obj.reference} {obj.display}")
            else:
                # logging.info(f"{obj.reference} found in G {g[obj.reference]}")
                obj.reference = g[obj.reference]

            # add global_reference to bundle
            if obj.reference in is_global and obj.reference not in add_to_research_study_bundle:
                add_to_research_study_bundle.append(obj.reference)

        if iterator:
            if id(obj) not in memo:
                memo.add(id(obj))
                for path_component, value in iterator(obj):
                    for result in obj_walk(value, path + (path_component,), memo):
                        yield result
                memo.remove(id(obj))
        else:
            yield path, obj

    for e in bundle.entry:
        for _, _ in obj_walk(e.resource):
            pass

    return bundle, add_to_research_study_bundle


def _file_attributes(file_name):
    """Calculate the hash and size."""
    import hashlib

    md5_hash = hashlib.md5()

    with open(file_name, "rb") as f:
        # Read and update hash in chunks of 4K
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)

    return md5_hash.hexdigest(), os.lstat(file_name).st_size


def _transform_bundle(file_path: Path, output_path: Path, global_resources: list) -> dict:
    """Read json, update bundle with bundle Specimen, Task, ensure DocumentReference."""
    tic = time.perf_counter()
    bundle = Bundle(json.load(open(file_path)))
    dna_diagnostic_reports = []
    dna_document_references = []
    imaging_diagnostic_reports = []
    imaging_document_references = []
    patient = None
    additional_entries = []
    conditions = []
    imaging_studies = []
    for e in bundle.entry:

        if e.resource.resource_type == 'Patient':
            patient = e.resource

        if e.resource.resource_type == 'ExplanationOfBenefit':
            if e.resource.status == 'completed':
                e.resource.status = None
                logging.warning(f"invalid status ExplanationOfBenefit.{e.resource.id}  set to None")

        if e.resource.resource_type == 'DiagnosticReport':
            codes = [c.code for c in e.resource.code.coding]
            # genetic panel
            if '55232-3' in codes:
                dna_diagnostic_reports.append(e.resource)
            # imaging
            if e.resource.presentedForm and len(e.resource.presentedForm) == 1 and e.resource.presentedForm[0].data:
                data = base64.b64decode(e.resource.presentedForm[0].data).decode("utf-8")
                if '.dcm' in data:
                    imaging_diagnostic_reports.append(e.resource)

        if e.resource.resource_type == 'DocumentReference':
            data = base64.b64decode(e.resource.content[0].attachment.data).decode("utf-8")
            if "_dna.csv" in data:
                dna_document_references.append(e.resource)
            if '.dcm' in data:
                imaging_document_references.append(e.resource)

        if e.resource.resource_type == 'Condition':
            conditions.append(e.resource)

        if e.resource.resource_type == 'ImagingStudy':
            imaging_studies.append(e.resource)

    output_file = None
    if len(dna_diagnostic_reports) > 0:
        # logging.info(f"{file_path} has {len(dna_diagnostic_reports)} genetic analysis reports")
        for diagnostic_report in dna_diagnostic_reports:
            # create a specimen
            specimen = Specimen()
            specimen.id = str(uuid.uuid5(uuid.UUID(diagnostic_report.id), 'specimen'))
            specimen.text = Narrative({"div": "Autogenerated specimen. Inserted to make data model research friendly.", "status": "generated"})
            specimen.subject = diagnostic_report.subject
            # add a reference to it back to the diagnostic report
            specimen_reference = FHIRReference({'reference': f"{specimen.resource_type}/{specimen.id}"})
            diagnostic_report.specimen = [specimen_reference]
            # add the specimen back to bundle
            additional_entries.append(specimen)
            # create a Task
            task = Task()
            task.id = str(uuid.uuid5(uuid.UUID(diagnostic_report.id), 'task'))
            task.text = Narrative({"div": "Autogenerated task. Inserted to make data model research friendly.", "status": "generated"})
            task.input = [TaskInput({'type': {'coding': [{'code': 'specimen'}]}, 'valueReference': specimen_reference.as_json()})]
            task.focus = specimen_reference
            task.for_fhir = diagnostic_report.subject
            task.output = [TaskOutput(
                {'type': {'coding': [{'code': diagnostic_report.resource_type}]},
                 'valueReference': {'reference': f"{diagnostic_report.resource_type}/{diagnostic_report.id}"}}
            )]
            assert len(
                dna_document_references) == 1, "Should have found a document reference with a reference to the dna data."
            # this document reference is the clinical note
            document_reference = dna_document_references[0]
            # clone the document reference, create new one with url
            document_reference_with_url = DocumentReference(document_reference.as_json())
            data = base64.b64decode(document_reference_with_url.content[0].attachment.data).decode("utf-8")
            lines = data.split('\n')
            line_with_file_info = next(
                iter([line for line in lines if 'genetic analysis summary panel  stored in' in line]), None)
            assert line_with_file_info
            path_from_report = line_with_file_info.split(' ')[-1]
            assert '_dna.csv' in path_from_report, f"{file_path}\n{data}\n{line_with_file_info}"
            # alter attachment
            document_reference_with_url.content[0].attachment.data = None
            document_reference_with_url.content[0].attachment.url = path_from_report
            md5, file_size = _file_attributes(path_from_report)
            document_reference_with_url.content[0].attachment.size = file_size
            if not document_reference_with_url.content[0].attachment.extension:
                document_reference_with_url.content[0].attachment.extension = []
            document_reference_with_url.content[0].attachment.extension.append(
                Extension({
                    "url": "http://aced-idp.org/fhir/StructureDefinition/md5",
                    "valueString": md5
                    }
                )
            )

            #
            # unique id
            additional_entries.append(document_reference_with_url)
            document_reference_with_url.id = str(uuid.uuid5(uuid.UUID(diagnostic_report.id), 'document_reference_with_url'))
            # add it to task
            task.output = [TaskOutput(
                {'type': {'coding': [{'code': document_reference_with_url.resource_type}]},
                 'valueReference': {'reference': f"{document_reference_with_url.resource_type}/{document_reference_with_url.id}"}}
            )]
            task.status = "completed"
            task.intent = "order"
            # add the task to bundle
            additional_entries.append(task)

    if len(imaging_diagnostic_reports) > 0:
        for imaging_diagnostic_report in imaging_diagnostic_reports:
            if len(imaging_document_references) != 1:
                logging.warning(f"No document reference found with a reference to the imaging data. {file_path}")
                continue

            # if len(imaging_studies) != len(imaging_document_references):
            #     logging.info(f"{file_path} has {len(imaging_studies)} imaging studies and "
            #                  f"{len(imaging_document_references)} document_references with embedded dicom files")

            # this document reference is the clinical note
            document_reference = imaging_document_references[0]
            # clone the document reference, create new one with url
            document_reference_with_url = DocumentReference(document_reference.as_json())
            data = base64.b64decode(document_reference_with_url.content[0].attachment.data).decode("utf-8")
            lines = data.split('\n')
            line_with_file_info = next(
                iter([line for line in lines if 'stored in' in line]), None)
            assert line_with_file_info
            path_from_report = line_with_file_info.split(' ')[-1]
            assert '.dcm' in path_from_report, f"{file_path}\n{data}\n{line_with_file_info}"
            # alter attachment

            document_reference_with_url.content[0].attachment.data = None
            document_reference_with_url.content[0].attachment.url = path_from_report
            md5, file_size = _file_attributes(path_from_report)
            document_reference_with_url.content[0].attachment.size = file_size
            if not document_reference_with_url.content[0].attachment.extension:
                document_reference_with_url.content[0].attachment.extension = []
            document_reference_with_url.content[0].attachment.extension.append(
                Extension({
                    "url": "http://aced-idp.org/fhir/StructureDefinition/md5",
                    "valueString": md5
                    }
                )
            )

            # unique id
            document_reference_with_url.id = str(uuid.uuid5(uuid.UUID(imaging_diagnostic_report.id), 'document_reference_with_url'))
            additional_entries.append(document_reference_with_url)
            # logging.info(f"Added dicom document_reference to bundle in {file_path}")

    # add entries to bundle
    for additional_entry in additional_entries:
        bundle_entry = BundleEntry()
        bundle_entry.resource = additional_entry
        bundle.entry.append(bundle_entry)

    # clean up references, make them ready for load
    bundle, add_to_research_study_bundle = _normalize_references(bundle, global_resources, file_path)

    # write new bundle to output
    output_file = output_path.joinpath(file_path.name)
    json.dump(bundle.as_json(), open(output_file, "w"))

    toc = time.perf_counter()
    msg = f"Parsed {file_path} in {toc - tic:0.4f} seconds, wrote {output_file}"
    logging.getLogger(__name__).info(msg)

    return {
        'patient_id': patient.id,
        'conditions': [condition.code.coding[0] for condition in conditions],
        'bundle_file_path': output_file,
        'add_to_research_study_bundle': add_to_research_study_bundle
    }


class StudyManifest(BaseModel):
    class Config:
        arbitrary_types_allowed = True
    study: ResearchStudy
    bundle_file_paths: List[str]


def create_study_manifests() -> dict:
    """Create a dict of studies."""
    # system, code, text
    conditions = """
http://snomed.info/sct,7200002,Alcoholism
http://snomed.info/sct,26929004,Alzheimer's disease (disorder)
http://snomed.info/sct,230265002,Familial Alzheimer's disease of early onset (disorder)
http://snomed.info/sct,44054006,Diabetes
http://snomed.info/sct,254837009,Malignant neoplasm of breast (disorder)
http://snomed.info/sct,363406005,Malignant tumor of colon
http://snomed.info/sct,68496003,Polyp of colon
http://snomed.info/sct,314994000,Metastasis from malignant tumor of prostate (disorder)
http://snomed.info/sct,126906006,Neoplasm of prostate
http://snomed.info/sct,92691004,Carcinoma in situ of prostate (disorder)
http://snomed.info/sct,424132000,Non-small cell carcinoma of lung,TNM stage 1 (disorder)
http://snomed.info/sct,254637007,Non-small cell lung cancer (disorder)
http://snomed.info/sct,162573006,Suspected lung cancer (situation)
    """.split('\n')
    conditions = [c.split(',') for c in conditions]
    conditions = {c[1]: {"coding": [
            {
              "code": c[1],
              "display": c[2],
              "system": c[0]
            }
          ],
          "text": c[2]} for c in conditions if len(c) > 1}

    study_manifests = {
        'Alcoholism': {'conditions': ['7200002']},
        "Alzheimers": {'conditions': ['26929004', '230265002']},
        "Diabetes": {'conditions': ['44054006']},
        "Breast Cancer": {'conditions': ['254837009']},
        "Colon Cancer": {'conditions': ['363406005', '68496003']},
        "Prostrate Cancer": {'conditions': ['314994000', '126906006', '92691004']},
        "Lung Cancer": {'conditions': ['254637007', '424132000', '162573006']},
    }
    for study_name, study_dict in study_manifests.items():
        study_dict['bundle_file_paths'] = []
        study_dict['research_subjects'] = []
        study_dict['research_study'] = ResearchStudy({
            'title': study_name,
            'id': str(uuid.uuid5(uuid.NAMESPACE_DNS, study_name)),
            'status': 'active',
            # 'primaryPurposeType': 'health-services-research',
            'description': 'An aggregation of patients from the Coherent Data Set  ' 
                           f'https://doi.org/10.1093/jamia/ocx079 that had conditions related to {study_name}. ',
            'condition': [
                condition for condition_code, condition in conditions.items() if
                condition_code in study_dict['conditions']
            ],
        })
        study_dict['add_to_research_study_bundle'] = []

    return study_manifests


def member_of_study(patient_conditions, study_manifests):
    """Add patient to manifest based on condition_code."""
    for patient_condition in patient_conditions['conditions']:
        for study in study_manifests.values():
            if patient_condition.code in study['conditions']:
                bundle_file_path = str(patient_conditions['bundle_file_path'])
                if bundle_file_path not in study['bundle_file_paths']:
                    logging.info(
                        f"Added {patient_conditions['bundle_file_path']} to research_study "
                        f"\"{study['research_study'].title}\" condition \"{patient_condition.display}\""
                    )
                    study['bundle_file_paths'].append(bundle_file_path)
                    study['research_subjects'].append(ResearchSubject(
                        {
                            'id': str(uuid.uuid5(uuid.NAMESPACE_DNS, patient_conditions['patient_id'])),
                            'status': 'on-study',
                            'study': {"reference": f"ResearchStudy/{study['research_study'].id}"},
                            'individual': {"reference": f"Patient/{patient_conditions['patient_id']}"},
                            'meta': {'source': bundle_file_path}
                        }
                    ))
                    study['add_to_research_study_bundle'].extend(
                        [
                            reference for reference in
                            patient_conditions['add_to_research_study_bundle']
                            if reference not in study['add_to_research_study_bundle']
                        ]
                    )


@click.command()
@click.option('--coherent_path',
              default='coherent/',
              show_default=True,
              help='Path to unzipped coherent data - see http://hdx.mitre.org/downloads/coherent-08-10-2021.zip.')
@click.option('--file_name_pattern',
              default='*.json',
              show_default=True,
              help='File names to match.')
@click.option('--minimum_file_count',
              default=1200,
              show_default=True,
              help='Minimum number of files.')
@click.option('--output_path',
              default='output/',
              show_default=True,
              help='Path to output data.')
def ingest(coherent_path, output_path, file_name_pattern, minimum_file_count):
    """Re-writes synthea bundles."""

    # validate parameters
    assert os.path.isdir(coherent_path)
    assert os.path.isdir(output_path)
    output_path = Path(output_path)
    fhir_path = Path(os.path.join(coherent_path, "output", "fhir"))
    assert os.path.isdir(fhir_path)

    # get coherent files
    file_paths = list(fhir_path.glob(file_name_pattern))
    assert len(file_paths) >= minimum_file_count, f"{str(fhir_path)}.{file_name_pattern} only returned"
    f"{len(file_paths)} expected at least {minimum_file_count}"

    # set up multi-processing
    tic = time.perf_counter()
    pool_count = max(multiprocessing.cpu_count() - 1, 1)
    pool = multiprocessing.Pool(pool_count)
    # organizations, locations, etc.
    global_resources = create_global_resources(coherent_path)
    # create our artificial ResearchStudies
    study_manifests = create_study_manifests()
    # transform patient bundles, write to output
    for patient_conditions in pool.starmap(_transform_bundle,
                                           zip(file_paths, repeat(output_path), repeat(global_resources))):
        # create ResearchStudy & ResearchSubject->Patient for each condition
        member_of_study(patient_conditions, study_manifests)
    toc = time.perf_counter()
    msg = f"Parsed all files in {fhir_path} in {toc - tic:0.4f} seconds"
    logging.getLogger(__name__).info(msg)

    def _make_bundle_entry(resource_):
        """Create BundleEntry with assigned resource"""
        _bundle_entry = BundleEntry()
        _bundle_entry.resource = resource_
        return _bundle_entry

    # # write out global_resources
    # for resource in global_resources:
    #     bundle = Bundle({'entry': [], 'type': 'collection'})
    #     bundle.entry.append(_make_bundle_entry(resource))
    # bundle_path = f"{output_path}/global_resources.json"
    # json.dump(bundle.as_json(), open(bundle_path, 'w'))
    # logging.getLogger(__name__).info(f"Created {bundle_path}")

    # write out research study bundles
    for study_manifest in study_manifests.values():
        bundle = Bundle({'entry': [], 'type': 'collection'})
        research_study = study_manifest['research_study']
        research_subjects = study_manifest['research_subjects']

        bundle.entry.append(_make_bundle_entry(research_study))
        bundle.entry.extend(
            [
                _make_bundle_entry(research_subject)
                for research_subject in research_subjects
            ]
        )

        for resource in global_resources:
            bundle_entry = BundleEntry()
            bundle_entry.resource = resource
            bundle.entry.append(bundle_entry)

        # for reference in study_manifest['add_to_research_study_bundle']:
        #     resource_type, id_ = reference.split('/')
        #     for resource in global_resources:
        #         if resource.resource_type == resource_type and resource.id == id_:
        #             logging.getLogger(__name__).debug(f"add global_reference {reference} to research_study_bundle")
        #             bundle_entry = BundleEntry()
        #             bundle_entry.resource = resource
        #             bundle.entry.append(bundle_entry)

        bundle_path = f"{output_path}/research_study_{'_'.join(research_study.title.split(' '))}.json"
        json.dump(bundle.as_json(), open(bundle_path, 'w'))
        logging.getLogger(__name__).info(f"Created {bundle_path}")

    # study_manifests_path = f"{output_path}/study_manifests.json"
    # json.dump(study_manifests, open(study_manifests_path, 'w'))
    # logging.getLogger(__name__).info(f"Created {study_manifests_path}")


if __name__ == '__main__':
    ingest()
