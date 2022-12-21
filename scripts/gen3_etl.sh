#!/bin/bash

# extract data from fhir service,
# transform it into gen3 'graph' and 'flat' models,
# load into Gen3 metadata_db and elastic search

# download if necessary
if [ ! -d "studies" ]
then
    echo "Directory studies DOES NOT exist. Fetching from FHIR server..."
    python3 scripts/coherent_fhir_studies.py extract
fi

# transform into gen3 graph form, will skip study if already done.
echo "Transforming into Gen3 graph form."
#rm -r studies/Alcoholism/extractions
#rm -r studies/Alzheimers/extractions
#rm -r studies/Breast_Cancer/extractions
#rm -r studies/Colon_Cancer/extractions
#rm -r studies/Diabetes/extractions
#rm -r studies/Lung_Cancer/extractions
#rm -r studies/Prostate_Cancer/extractions
# intentionally set the ids relative to study, allows entities to be "duplicated" in separate projects
python3 scripts/gen3_emitter.py data transform --ids_relative_to_study

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
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Alcoholism
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Alzheimers
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Breast_Cancer
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Colon_Cancer
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Diabetes
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Lung_Cancer
nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code Prostate_Cancer


#nice -10 scripts/gen3_emitter.py data load --db_host localhost --sheepdog_creds_path ../compose-services/Secrets/sheepdog_creds.json --project_code HOP --input_path /Users/walsbr/hop/data-etl/data/fhir


# load metadata to elastic

python3 scripts/load_elastic.py --project_id aced-Alcoholism --index observation --path studies/Alcoholism/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Alzheimers --index observation --path studies/Alzheimers/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Breast_Cancer --index observation --path studies/Breast_Cancer/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Colon_Cancer --index observation --path studies/Colon_Cancer/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Diabetes --index observation --path studies/Diabetes/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Lung_Cancer --index observation --path studies/Lung_Cancer/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Prostate_Cancer --index observation --path studies/Prostate_Cancer/extractions/Observation.ndjson

