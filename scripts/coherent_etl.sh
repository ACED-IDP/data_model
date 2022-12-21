#!/bin/bash

# extract from mitre,
# transform adjust for our purposes,
# load into FHIR service, assign cohorts to studies

# download if necessary
if [ ! -d "output" ]
then
    echo "Directory output DOES NOT exist. Fetching ..."
    wget http://hdx.mitre.org/downloads/coherent-11-17-2022.zip
    # writes files into output/
    unzip coherent-11-17-2022.zip
fi

# ingest assumes coherent downloaded and unzipped

# fix the document_reference, genomic observation
python3 scripts/coherent_refactor_bundle.py

# load to fhir service
nice -10 python3 scripts/coherent_fhir_load.py --chunk_size 3

# assign patients to study
python3 scripts/coherent_fhir_studies.py create

# list studies and counts
sleep 10
python3 scripts/coherent_fhir_studies.py ls



