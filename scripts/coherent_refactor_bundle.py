import asyncio
import base64
import json
import logging
import os
import pathlib
import time
import uuid
from pathlib import Path
from typing import Iterator, Dict
import unicodedata
import click
from fhirclient.models.bundle import Bundle, BundleEntry, BundleEntryRequest
from fhirclient.models.codeableconcept import CodeableConcept
from fhirclient.models.documentreference import DocumentReference
from fhirclient.models.extension import Extension
from fhirclient.models.fhirreference import FHIRReference
from fhirclient.models.narrative import Narrative
from fhirclient.models.observation import Observation, ObservationComponent
from fhirclient.models.specimen import Specimen
from fhirclient.models.task import Task, TaskInput, TaskOutput

logging.basicConfig(format='%(asctime)s %(message)s',  encoding='utf-8', level=logging.INFO)
logger = logging.getLogger(__name__)


def _redact_file_name(path, patient, coherent_path):
    """Remove patient name from path, assumes given_family_XXXX.XXXX"""
    file_name = path.split('/')[-1]
    first_part = file_name.split('_')[:1][0].lower()
    given_name = patient.name[0].given[0].lower()
    if given_name != first_part:
        return path
    redacted_file_name = '_'.join(path.split('_')[2:])
    return path.replace(file_name, redacted_file_name)


def _file_attributes(file_name):
    """Calculate the hash and size."""
    import hashlib

    md5_hash = hashlib.md5()
    file_name = unicodedata.normalize("NFKD", file_name)
    with open(file_name, "rb") as f:
        # Read and update hash in chunks of 4K
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)

    return md5_hash.hexdigest(), os.lstat(file_name).st_size


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


def _genomic_observation(observation) -> Observation:
    """Transform dict into a Genomic Implication Observation."""
    # fix missing data
    # if 'status' not in observation_dict['resource'] or not observation_dict['resource']['status']:
    #     observation_dict['resource']['status'] = 'preliminary'
    if not observation.status:
        observation.status = 'preliminary'
    # load into a python object
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
        'coding': [
            {
                "system": "http://www.genenames.org/geneId",
                "code": implication['gene'],
                "display": implication['gene']
            },
            {
                "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
                "code": "diagnostic-implication",
                "display": "diagnostic-implication"
            }
        ]
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
                    "code": implication['significance'],  # f"TODO lookup code for: ",
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
                    "code": condition,  # f"TODO - lookup snomed code for {condition}",
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
    return observation


def _transform_document_reference(bundle: Bundle, coherent_path: Path) -> Bundle:
    """Get file attributes, update bundle with bundle Specimen, Task, ensure DocumentReference.
    see https://github.com/ACED-IDP/data_model/issues/20
    """
    dna_diagnostic_reports = []
    dna_document_references = []
    imaging_diagnostic_reports = []
    imaging_document_references = []
    clinical_note_references = []
    patient = None
    additional_entries = []
    conditions = []
    imaging_studies = []
    for e in bundle.entry:

        if e.resource.resource_type == 'Observation' and e.resource.code.coding[0].code == '69548-6':
            e.resource = _genomic_observation(e.resource)

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

        if e.resource.resource_type == 'DocumentReference' and e.resource.content[0].attachment.data:
            data = base64.b64decode(e.resource.content[0].attachment.data).decode("utf-8")
            if "_dna.csv" in data:
                dna_document_references.append(e.resource)
                clinical_note_references.append(e.resource)
            elif '.dcm' in data:
                imaging_document_references.append(e.resource)
                clinical_note_references.append(e.resource)
            else:
                clinical_note_references.append(e.resource)

        if e.resource.resource_type == 'Condition':
            conditions.append(e.resource)

        if e.resource.resource_type == 'ImagingStudy':
            imaging_studies.append(e.resource)

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
            assert len(dna_document_references) == 1, \
                f"Should have found a document reference with a reference to the dna data. {len(dna_document_references)}"
            # this document reference is the clinical note
            document_reference = dna_document_references[0]
            # clone the document reference, create new one with url
            document_reference_with_url = DocumentReference(document_reference.as_json())

            document_reference_with_url.status = "current"
            document_reference_with_url.category[0].coding[0].system = "http://loinc.org"
            document_reference_with_url.category[0].coding[0].code = "100029-8"
            document_reference_with_url.category[0].coding[0].display = "Cancer related multigene analysis Molgen Doc (cfDNA)"

            data = base64.b64decode(document_reference_with_url.content[0].attachment.data).decode("utf-8")
            lines = data.split('\n')
            line_with_file_info = next(
                iter([line for line in lines if 'genetic analysis summary panel  stored in' in line]), None)
            assert line_with_file_info
            path_from_report = line_with_file_info.split(' ')[-1]
            path_from_report = coherent_path + path_from_report.replace('./output', '')  # adjust to our unzipped location

            assert '_dna.csv' in path_from_report, f"{data} {line_with_file_info}"

            # remove name from file
            redacted_path_from_report = _redact_file_name(path_from_report, patient, coherent_path=coherent_path)
            if redacted_path_from_report != path_from_report:
                # not the first run, we've already redacted
                if not pathlib.Path(redacted_path_from_report).is_file():
                    # need this on linux? https://nedbatchelder.com/blog/201106/filenames_with_accents.html
                    path_from_report = unicodedata.normalize("NFKD", path_from_report)                    
                    pathlib.Path(path_from_report).rename(redacted_path_from_report)
                    #os.rename(path_from_report, redacted_path_from_report)
                path_from_report = redacted_path_from_report

            # alter attachment
            document_reference_with_url.content[0].attachment.data = None
            document_reference_with_url.content[0].attachment.url = path_from_report

            # decorate with md5, file_size
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
                logging.warning(f"No document reference found with reference to imaging.  patient {patient.id}")
                continue

            # if len(imaging_studies) != len(imaging_document_references):
            #     logging.info(f"{file_path} has {len(imaging_studies)} imaging studies and "
            #                  f"{len(imaging_document_references)} document_references with embedded dicom files")

            # this document reference is the clinical note
            document_reference = imaging_document_references[0]
            # clone the document reference, create new one with url
            document_reference_with_url = DocumentReference(document_reference.as_json())

            document_reference_with_url.status = "current"
            document_reference_with_url.category[0].coding[0].system = "http://terminology.hl7.org/CodeSystem/media-category"
            document_reference_with_url.category[0].coding[0].code = "image"
            document_reference_with_url.category[0].coding[0].display = "Image"

            data = base64.b64decode(document_reference_with_url.content[0].attachment.data).decode("utf-8")
            lines = data.split('\n')
            line_with_file_info = next(
                iter([line for line in lines if 'stored in' in line]), None)
            assert line_with_file_info
            path_from_report = line_with_file_info.split(' ')[-1]
            path_from_report = coherent_path + path_from_report.replace('./output', '')  # adjust to our unzipped location

            assert '.dcm' in path_from_report, f"{data} {line_with_file_info}"
            # alter attachment

            # remove name from file
            redacted_path_from_report = _redact_file_name(path_from_report, patient, coherent_path)
            if redacted_path_from_report != path_from_report:
                # not the first run, we've already redacted
                if not pathlib.Path(redacted_path_from_report).is_file():
                    # need this on linux? https://nedbatchelder.com/blog/201106/filenames_with_accents.html
                    path_from_report = unicodedata.normalize("NFKD", path_from_report)
                    pathlib.Path(path_from_report).rename(redacted_path_from_report)
                    #os.rename(path_from_report, redacted_path_from_report)
                path_from_report = redacted_path_from_report

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

    if len(clinical_note_references) > 0:
        for document_reference in clinical_note_references:
            # write data as a file
            data = base64.b64decode(document_reference.content[0].attachment.data).decode("utf-8")
            data = data.replace(patient.name[0].given[0], '')
            path = f"{coherent_path}/clinical_reports/{patient.id}_{document_reference.id}.txt"
            pathlib.Path(f"{coherent_path}/clinical_reports").mkdir(exist_ok=True)
            with open(path, "w") as f:
                f.write(data)
            # alter attachment
            document_reference.content[0].attachment.data = None
            document_reference.content[0].attachment.url = path
            md5, file_size = _file_attributes(path)
            document_reference.content[0].attachment.size = file_size
            if not document_reference.content[0].attachment.extension:
                document_reference.content[0].attachment.extension = []
            document_reference.content[0].attachment.extension.append(
                Extension({
                    "url": "http://aced-idp.org/fhir/StructureDefinition/md5",
                    "valueString": md5
                    }
                )
            )

    # add entries to bundle
    for additional_entry in additional_entries:
        bundle_entry = BundleEntry()
        bundle_entry.resource = additional_entry
        bundle_entry.request = BundleEntryRequest()
        bundle_entry.request.method = 'PUT'
        bundle_entry.request.url = f"{bundle_entry.resource.resource_type}/{bundle_entry.resource.id}"
        bundle.entry.append(bundle_entry)

    # assume fhir server will clean up references, i.e. make them ready for load
    return bundle


async def transform(path, coherent_path):
    """Read a bundle, transform it/fix it, save it in place."""
    tic = time.perf_counter()
    with open(path, 'r') as fp:
        bundle = Bundle(json.loads(fp.read()))
        bundle = _transform_document_reference(bundle, coherent_path=coherent_path)
        # bundle = _transform_observation(bundle)
    # trigger as_json
    transformed = json.dumps(bundle.as_json(), separators=(',', ':'))
    bundle = None
    with open(path, 'w') as fp:
        fp.write(transformed)
    toc = time.perf_counter()
    logger.info(f"READ and TRANSFORMED {path} {toc - tic:0.4f} seconds")
    return True


def _chunker(seq: Iterator, size: int) -> Iterator:
    """Iterate over a list in chunks.

    Args:
        seq: an iterable
        size: desired chunk size

    Returns:
        an iterator that returns lists of size or less
    """
    return (seq[pos:pos + size] for pos in range(0, len(seq), size))


async def transform_all(coherent_path):
    """Load all the bundles"""

    # get all the patients
    paths = sorted([p for p in Path(f'{coherent_path}/fhir/').glob('*.json') if
                    'organizations' not in str(p) and 'practitioners' not in str(p)])

    # doing this as a maximum of 3 seems to work when combined with nice -10 on a laptop
    limit = None
    count = 0
    ok = False
    for chunk in _chunker(paths, 3):
        tasks = []

        for path in chunk:
            task = asyncio.create_task(transform(path=path, coherent_path=coherent_path))
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

    assert ok, "Did not transform bundles"


@click.command()
@click.option('--coherent_path', default='output', show_default=True,
              help='Unzipped directory: see http://hdx.mitre.org/downloads/coherent-11-07-2022.zip')
def main(coherent_path):
    """Adjust DocumentReferences see https://github.com/ACED-IDP/data_model/issues/20"""
    asyncio.run(transform_all(coherent_path))
    logger.info('done')


if __name__ == '__main__':
    main()
