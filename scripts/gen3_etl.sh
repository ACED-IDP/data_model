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
    python3 scripts/etl.py schema --output_path generated-json-schema
    python3 scripts/etl.py schema-publish --dictionary_path generated-json-schema/aced.json
fi

# transform into gen3 graph form, will skip study if already done.
echo "Transforming into Gen3 graph form."
synthetic_studies=(Alcoholism Alzheimers Breast_Cancer Colon_Cancer Diabetes Lung_Cancer Prostate_Cancer)
all_studies=(Alcoholism Alzheimers Breast_Cancer Colon_Cancer Diabetes Lung_Cancer Prostate_Cancer HOP)


for study in ${synthetic_studies[*]}; do
  # setup directories for extract
  rm -r studies/$study/extractions || true
  mkdir -p studies/$study/extractions
  # TODO this is too slow
  # intentionally set the ids relative to study, allows entities to be "duplicated" in separate projects
  python3 scripts/etl.py transform --input_path studies/$study/  --output_path studies/$study/extractions  --duplicate_ids_for $study
done


# also do HOP - no duplicates
rm -r studies/HOP/extractions || true
mkdir -p studies/HOP/extractions
python3 scripts/etl.py transform --input_path ~/hop/data-etl/data/fhir/HOP --output_path studies/HOP/extractions

# clear existing data
echo "Truncating gen3 tables."
cat scripts/truncate_sheepdog_tables.sql |  docker exec -i  postgres psql -U postgres
# clear existing data
echo "Truncating buckets."
cat scripts/truncate_indexd_tables.sql |  docker exec -i  postgres psql -U postgres
cat scripts/truncate_buckets.sh |  docker exec -i  etl-service bash


# upload will start multiple processes to submit files to bucket and update studies DocumentReferences
nice -n 10 scripts/upload-files Alcoholism aced-ohsu
nice -n 10 scripts/upload-files Alzheimers aced-ucl
nice -n 10 scripts/upload-files Breast_Cancer manchester
nice -n 10 scripts/upload-files Colon_Cancer aced-stanford
nice -n 10 scripts/upload-files Diabetes aced-ucl
nice -n 10 scripts/upload-files Lung_Cancer aced-manchester
nice -n 10 scripts/upload-files Prostate_Cancer aced-stanford

# upload meta data to gen3
for study in ${synthetic_studies[*]}; do
  nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code $study
done

nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code HOP

# load metadata to elastic

# Delete all from ES
# docker exec esproxy-service curl -X DELETE http://localhost:9200/gen3.aced.*

for study in ${all_studies[*]}; do
  nice -10 python3 scripts/load_elastic.py --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson
done

for study in ${synthetic_studies[*]}; do
  nice -10 python3 scripts/load_elastic.py --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson
done

for study in ${all_studies[*]}; do
  nice -10 python3 scripts/load_elastic.py --project_id aced-$study --index observation --path studies/$study/extractions/Observation.ndjson
done

python3 scripts/load_elastic.py --project_id aced-Alcoholism --index observation --path studies/Alcoholism/extractions/Observation.ndjson
#python3 scripts/load_elastic.py --project_id aced-Alzheimers --index observation --path studies/Alzheimers/extractions/Observation.ndjson
#python3 scripts/load_elastic.py --project_id aced-Breast_Cancer --index observation --path studies/Breast_Cancer/extractions/Observation.ndjson
#python3 scripts/load_elastic.py --project_id aced-Colon_Cancer --index observation --path studies/Colon_Cancer/extractions/Observation.ndjson
#python3 scripts/load_elastic.py --project_id aced-Diabetes --index observation --path studies/Diabetes/extractions/Observation.ndjson
#python3 scripts/load_elastic.py --project_id aced-Lung_Cancer --index observation --path studies/Lung_Cancer/extractions/Observation.ndjson
#python3 scripts/load_elastic.py --project_id aced-Prostate_Cancer --index observation --path studies/Prostate_Cancer/extractions/Observation.ndjson

python3 scripts/load_elastic.py --project_id aced-HOP --index observation --path studies/HOP/extractions/Observation.ndjson


python3 scripts/load_elastic.py --project_id aced-Alcoholism --index file --path studies/Alcoholism/extractions/DocumentReference.ndjson
#python3 scripts/load_elastic.py --project_id aced-Alzheimers --index file --path studies/Alzheimers/extractions/DocumentReference.ndjson
#python3 scripts/load_elastic.py --project_id aced-Breast_Cancer --index file --path studies/Breast_Cancer/extractions/DocumentReference.ndjson
#python3 scripts/load_elastic.py --project_id aced-Colon_Cancer --index file --path studies/Colon_Cancer/extractions/DocumentReference.ndjson
#python3 scripts/load_elastic.py --project_id aced-Diabetes --index file --path studies/Diabetes/extractions/DocumentReference.ndjson
#python3 scripts/load_elastic.py --project_id aced-Lung_Cancer --index file --path studies/Lung_Cancer/extractions/DocumentReference.ndjson
#python3 scripts/load_elastic.py --project_id aced-Prostate_Cancer --index file --path studies/Prostate_Cancer/extractions/DocumentReference.ndjson
