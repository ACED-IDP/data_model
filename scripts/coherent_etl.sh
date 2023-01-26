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

# if this is a local, development re-processing of data
# clear *ALL* data from hapi server
# cat scripts/truncate_hapi_fhir_tables.sql |  docker exec -i  postgres psql -U postgres


# fix the document_reference, genomic observation
python3 scripts/coherent_refactor_bundle.py

# load to fhir service
# the fhir service will normalize all the `contained` and `uniq id` style references
# TODO this is very slow... performance needs to be improved, probably by loading ndjson not bundles https://smilecdr.com/docs/bulk/fhir_bulk_import.html
nice -10 python3 scripts/coherent_fhir_load.py --chunk_size 4

# assign patients to study within the FHIR service
python3 scripts/coherent_fhir_studies.py create

# list studies and counts
sleep 10
python3 scripts/coherent_fhir_studies.py ls



