import base64
import json
import multiprocessing
import os
import re
from pathlib import Path

import yaml
from fhirclient.models.bundle import Bundle


def _assert_legal_keys(keys):
    """Valid graphql key name"""
    illegal_keys = []
    for key in keys:
        if not re.match('^[_a-zA-Z][_a-zA-Z0-9]*$', key):
            illegal_keys.append(key)
        if '-' in key:
            illegal_keys.append(key)
    assert len(illegal_keys) == 0, illegal_keys


def _verify_bundle(output_file_path) -> str:
    """Verify output file."""
    assert os.path.isfile(output_file_path), "Should be a file."
    bundle = Bundle(json.load(open(output_file_path)))
    assert bundle, "Should parse json into Bundle"

    diagnostic_reports = []
    document_references = []
    dicom_document_references = []
    dicom_diagnostic_reports = []
    imaging_studies = []
    should_have_dicom_file = False
    for e in bundle.entry:
        _assert_legal_keys(vars(e.resource))
        if e.resource.resource_type == 'Patient':
            patient = e.resource
        if e.resource.resource_type == 'DiagnosticReport':
            codes = [c.code for c in e.resource.code.coding]
            # genetic panel
            if '55232-3' in codes:
                diagnostic_reports.append(e.resource)
            if e.resource.presentedForm and len(e.resource.presentedForm) == 1 and e.resource.presentedForm[0].data:
                data = base64.b64decode(e.resource.presentedForm[0].data).decode("utf-8")
                if '.dcm' in data:
                    dicom_diagnostic_reports.append(e.resource)

        if e.resource.resource_type == 'DocumentReference' and e.resource.content[0].attachment.data:
            data = base64.b64decode(e.resource.content[0].attachment.data).decode("utf-8")
            if '.dcm' in data:
                should_have_dicom_file = True

        if e.resource.resource_type == 'ImagingStudy':
            imaging_studies.append(e.resource)

        if e.resource.resource_type == 'DocumentReference':
            if e.resource.content[0].attachment.url and "_dna.csv" in e.resource.content[0].attachment.url:
                document_references.append(e.resource)
            if e.resource.content[0].attachment.url and ".dcm" in e.resource.content[0].attachment.url:
                dicom_document_references.append(e.resource)

        if e.resource.resource_type == 'CareTeam':
            for participant in e.resource.participant:
                assert 'uuid' not in participant.member.reference, (participant.member.reference, output_file_path)
                assert '?identifier=' not in participant.member.reference, (participant.member.reference, participant.member.display, output_file_path)

    if len(diagnostic_reports) > 0:
        assert len(document_references) == 1, \
            f"Should have a 1 document reference for each genomic panel each with a url. This one {output_file_path} had {len(document_references)}"

    if should_have_dicom_file:
        assert len(dicom_document_references) == 1, f"Should have 1 document reference for each diagnostic_reports with .dcm file. {output_file_path} {len(dicom_document_references)}"
        assert len(imaging_studies) > 0, f"Should have at least 1 imaging study for each diagnostic_reports with .dcm file. {output_file_path} {len(imaging_studies)}"
        assert dicom_diagnostic_reports[0].encounter.reference == dicom_document_references[0].context.encounter[0].reference

    # return a string so it can be serialized as across process boundaries
    return str(True)


def test_all_output_files(coherent_path, output_path, number_of_files_to_sample):
    """Ensure expected entities in output bundles."""
    fhir_path = Path(os.path.join(coherent_path, "output", "fhir"))
    assert os.path.isdir(fhir_path), "Input should exist"
    input_file_paths = list(fhir_path.glob('A*.json'))
    assert len(input_file_paths) > 0, "Should have at least one file from over 1200 files."

    assert os.path.isdir(output_path), "Output should exist"
    output_path = Path(output_path)

    output_file_paths = list(output_path.glob('*.json'))
    output_file_paths = [p for p in output_file_paths if p.name != 'research_studies.json']
    # assert len(input_file_paths) == len(output_file_paths), "Should have equal number of output files."

    if number_of_files_to_sample:
        output_file_paths = output_file_paths[:number_of_files_to_sample]

    pool_count = max(multiprocessing.cpu_count() - 1, 1)
    pool = multiprocessing.Pool(pool_count)
    results = pool.map(_verify_bundle, output_file_paths)
    assert all(["true" == r.lower() for r in results]), "All files should be OK."


def test_property_names(pfb_work_files_path):
    pfb_work_files_path = Path(pfb_work_files_path + "/gen3")
    assert len([file_name for file_name in pfb_work_files_path.glob("*.yaml")]) > 0
    for file_name in pfb_work_files_path.glob("*.yaml"):
        print(file_name)
        schema = yaml.load(open(file_name), Loader=yaml.SafeLoader)
        keys = [k for k in schema.keys() if k != '$schema']
        illegal_keys = []
        for key in keys:
            if not re.match('^[_a-zA-Z][_a-zA-Z0-9]*$', key):
                illegal_keys.append(key)
            if '-' in key:
                illegal_keys.append(key)
            if ':' in key:
                illegal_keys.append(key)
        for key in schema.get('properties', []):
            if not re.match('^[_a-zA-Z][_a-zA-Z0-9]*$', key):
                illegal_keys.append(key)
            if '-' in key:
                illegal_keys.append(key)
            if ':' in key:
                illegal_keys.append(key)
        assert len(illegal_keys) == 0, illegal_keys

