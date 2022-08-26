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


@click.command()
@click.option('--file_name_pattern',
              default='research_study*.json',
              show_default=True,
              help='File names to match.')
@click.option('--input_path',
              default='output/',
              show_default=True,
              help='Path to output data.')
def pfb(input_path, file_name_pattern):
    """Create PFB from coherent ResearchStudies."""

    # validate parameters
    assert os.path.isdir(input_path)
    input_path = Path(input_path)
    assert os.path.isdir(input_path)
    file_paths = list(input_path.glob(file_name_pattern))
    assert len(file_paths) >= 1, f"{str(input_path)}/{file_name_pattern} only returned {len(file_paths)} expected at least 1"

    for file_path in file_paths:
        bundle = Bundle(json.load(open(file_path)))
        research_subjects = [bundle_entry.resource for bundle_entry in bundle.entry if bundle_entry.resource.resource_type == "ResearchSubject"]
        sources = [research_subject.meta.source for research_subject in research_subjects]
        sources.append(file_path)
        input_paths = [f"--input_path \"{source}\" \\\n" for source in sources]
        pfb_file_name = f"{str(file_path).split('/')[-1].split('.')[0]}.pfb"
        cmd = f"pfb_fhir transform {' '.join(input_paths)} --pfb_path {pfb_file_name}"
        print(cmd)


if __name__ == '__main__':
    pfb()

