#!/bin/bash

# exit when any command fails
set -e

# extract data from fhir service,
# transform it into gen3 'flat' models,
# load into Gen3 metadata_db and elastic search


# download if necessary
if [ ! -d "studies" ]
then
    echo "Directory studies DOES NOT exist. Fetching from FHIR server..."
    # TODO - this needs improvement
    #  depends on FHIR service's `include/_revinclude` query returning *ALL* nodes
    #  the HAPI implementation changed this behavior and capped edge results to 1000
    #  other vendors return even less
    #  results in pinning HAPI service to "hapiproject/hapi:v6.1.0"
    python3 scripts/coherent_fhir_studies.py extract
fi


# create schema if necessary
if [ ! -d "generated-json-schema" ]
then
    python3 scripts/schema.py generate --output_path generated-json-schema
fi

# transform into gen3 graph form, will skip study if already done.
echo "Transforming into Gen3 graph form."
synthetic_studies=(Alcoholism Alzheimers Breast_Cancer Colon_Cancer Diabetes Lung_Cancer Prostate_Cancer)
real_studies=(NVIDIA)
all_studies=(Alcoholism Alzheimers Breast_Cancer Colon_Cancer Diabetes Lung_Cancer Prostate_Cancer HOP NVIDIA)


for study in ${synthetic_studies[*]}; do
  # setup directories for extract
  rm -r studies/$study/extractions || true
  mkdir -p studies/$study/extractions
#  # TODO this is too slow
  # intentionally set the ids relative to study, allows entities to be "duplicated" in separate projects
  python3 scripts/transform.py transform --input_path studies/$study/  --output_path studies/$study/extractions  --duplicate_ids_for $study
done


rm -r studies/HOP/extractions || true
mkdir -p studies/HOP/extractions
python3 scripts/transform.py transform --input_path ~/hop/data-etl/data/fhir/HOP --output_path studies/HOP/extractions

for study in ${real_studies[*]}; do
  mkdir -p studies/$study/extractions
  python3 scripts/transform.py transform --input_path studies/$study/  --output_path studies/$study/extraction
done

# clear existing data
echo "Truncating gen3 tables."
cat scripts/truncate_sheepdog_tables.sql |  docker exec -i  postgres psql -U postgres
# clear existing data
echo "Truncating buckets."
cat scripts/truncate_indexd_tables.sql |  docker exec -i  postgres psql -U postgres
cat scripts/truncate_buckets.sh |  docker exec -i  etl-service bash


# create program and projects based on resources found in user.yaml
python3 scripts/load.py init



# upload will start multiple processes to submit files to bucket and update studies DocumentReferences
nice -n 10 scripts/upload-files Alcoholism aced-ohsu-staging
nice -n 10 scripts/upload-files Alzheimers aced-ucl
nice -n 10 scripts/upload-files Breast_Cancer aced-manchester
nice -n 10 scripts/upload-files Colon_Cancer aced-stanford
nice -n 10 scripts/upload-files Diabetes aced-ucl
nice -n 10 scripts/upload-files Lung_Cancer aced-manchester
nice -n 10 scripts/upload-files Prostate_Cancer aced-stanford
nice -n 10 scripts/upload-files NVIDIA aced-ohsu


# upload meta data to gen3
for study in ${synthetic_studies[*]}; do
  nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code $study
done

for study in ${real_studies[*]}; do
  nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code $study
done

nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code HOP

# load metadata to elastic

# Delete all from ES
# docker exec esproxy-service curl -X DELETE http://localhost:9200/gen3.aced.*

for study in ${synthetic_studies[*]}; do
  rm patient.sqlite
  nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson
  nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson
  nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index observation --path studies/$study/extractions/Observation.ndjson
done


study=HOP
rm patient.sqlite
nice -10 python3 scripts/load.py load  flat --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson
nice -10 python3 scripts/load.py load  flat --project_id aced-$study --index observation --path studies/$study/extractions/Observation.ndjson

study=NVIDIA
rm patient.sqlite
nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson


rm patient.sqlite
nice -10 python3 scripts/load.py load  flat --project_id HOP-CORE --index patient --path studies/CORE/extractions/Patient.ndjson
nice -10 python3 scripts/load.py load  flat --project_id HOP-CORE --index observation --path studies/CORE/extractions/Observation.ndjson


# python3 scripts/schema.py publish  --production
