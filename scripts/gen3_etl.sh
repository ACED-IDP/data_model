#!/bin/bash

# extract data from fhir service,
# transform it into gen3 'graph' and 'flat' models,
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
# TODO this is too slow
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

python3 scripts/load_elastic.py --project_id aced-Alcoholism --index patient --path studies/Alcoholism/extractions/Patient.ndjson
python3 scripts/load_elastic.py --project_id aced-Alzheimers --index patient --path studies/Alzheimers/extractions/Patient.ndjson
python3 scripts/load_elastic.py --project_id aced-Breast_Cancer --index patient --path studies/Breast_Cancer/extractions/Patient.ndjson
python3 scripts/load_elastic.py --project_id aced-Colon_Cancer --index patient --path studies/Colon_Cancer/extractions/Patient.ndjson
python3 scripts/load_elastic.py --project_id aced-Diabetes --index patient --path studies/Diabetes/extractions/Patient.ndjson
python3 scripts/load_elastic.py --project_id aced-Lung_Cancer --index patient --path studies/Lung_Cancer/extractions/Patient.ndjson
python3 scripts/load_elastic.py --project_id aced-Prostate_Cancer --index patient --path studies/Prostate_Cancer/extractions/Patient.ndjson

python3 scripts/load_elastic.py --project_id aced-Alcoholism --index observation --path studies/Alcoholism/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Alzheimers --index observation --path studies/Alzheimers/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Breast_Cancer --index observation --path studies/Breast_Cancer/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Colon_Cancer --index observation --path studies/Colon_Cancer/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Diabetes --index observation --path studies/Diabetes/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Lung_Cancer --index observation --path studies/Lung_Cancer/extractions/Observation.ndjson
python3 scripts/load_elastic.py --project_id aced-Prostate_Cancer --index observation --path studies/Prostate_Cancer/extractions/Observation.ndjson

python3 scripts/load_elastic.py --project_id aced-Alcoholism --index file --path studies/Alcoholism/extractions/DocumentReference.ndjson
python3 scripts/load_elastic.py --project_id aced-Alzheimers --index file --path studies/Alzheimers/extractions/DocumentReference.ndjson
python3 scripts/load_elastic.py --project_id aced-Breast_Cancer --index file --path studies/Breast_Cancer/extractions/DocumentReference.ndjson
python3 scripts/load_elastic.py --project_id aced-Colon_Cancer --index file --path studies/Colon_Cancer/extractions/DocumentReference.ndjson
python3 scripts/load_elastic.py --project_id aced-Diabetes --index file --path studies/Diabetes/extractions/DocumentReference.ndjson
python3 scripts/load_elastic.py --project_id aced-Lung_Cancer --index file --path studies/Lung_Cancer/extractions/DocumentReference.ndjson
python3 scripts/load_elastic.py --project_id aced-Prostate_Cancer --index file --path studies/Prostate_Cancer/extractions/DocumentReference.ndjson
