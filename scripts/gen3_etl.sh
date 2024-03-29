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
  rm -r studies-5.0/$study || true
  mkdir -p studies-5.0/$study
  python3 scripts/transform.py migrate --input_path studies/$study/  --output_path studies-5.0/$study  --validate
  mv studies/$study/ studies-back/$study/
  mv studies-5.0/$study/ studies/$study/
done

study=HOP
rm -r studies/$study || true
mkdir -p studies/$study
python3 scripts/transform.py migrate --input_path ~/hop/data-etl/data/fhir/HOP --output_path studies/$study  --validate

study=MCF10A
rm -r studies/$study || true
mkdir -p studies/$study
python3 scripts/transform.py migrate --input_path ~/aced/MCF10A/output  --output_path studies/$study  --validate

study=nvidia
rm -r studies/$study || true
mkdir -p studies/$study
python3 scripts/transform.py migrate --input_path tmp/nvidia  --output_path studies/$study  --validate


for study in ${synthetic_studies[*]}; do
  # setup directories for extract
  rm -r studies/$study/extractions || true
  mkdir -p studies/$study/extractions
  # intentionally set the ids relative to study, allows entities to be "duplicated" in separate projects
  python3 scripts/transform.py transform --input_path studies/$study/  --output_path studies/$study/extractions  --duplicate_ids_for $study
done


rm -r studies/MCF10A/extractions || true
mkdir -p studies/MCF10A/extractions
python3 scripts/transform.py transform --input_path studies/MCF10A --output_path studies/MCF10A/extractions

rm -r studies/HOP/extractions || true
mkdir -p studies/HOP/extractions
python3 scripts/transform.py transform --input_path studies/HOP --output_path studies/HOP/extractions


rm -r studies/NVIDIA/extractions || true
mkdir -p studies/NVIDIA/extractions
python3 scripts/transform.py transform --input_path studies/nvidia --output_path studies/nvidia/extractions



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
USER_PATH='--user_path ../compose-services-training/Secrets/user.yaml'
# in etl pod
# unset USER_PATH
python3 scripts/load.py init $USER_PATH


# development/staging AWS & ACC buckets

export Alcoholism_BUCKET=aced-commons-data-bucket  # aced-ohsu-staging
export Alzheimers_BUCKET=aced-commons-ucl-data-bucket
export Breast_Cancer_BUCKET=aced-commons-manchester-data-bucket
export Colon_Cancer_BUCKET=aced-commons-stanford-data-bucket
export Diabetes_BUCKET=aced-commons-ucl-data-bucket
export Lung_Cancer_BUCKET=aced-commons-manchester-data-bucket
export Prostate_Cancer_BUCKET=aced-commons-stanford-data-bucket
export NVIDIA_BUCKET=aced-commons-data-bucket  # aced-ohsu-staging

# development minio docker-compose buckets
export Alcoholism_BUCKET=aced-ohsu
export Alzheimers_BUCKET=aced-ucl
export Breast_Cancer_BUCKET=aced-manchester
export Colon_Cancer_BUCKET=aced-stanford
export Diabetes_BUCKET=aced-ucl
export Lung_Cancer_BUCKET=aced-manchester
export Prostate_Cancer_BUCKET=aced-stanford
export NVIDIA_BUCKET=aced-ohsu

# upload will start multiple processes to submit files to bucket and update studies DocumentReferences
nice -n 10 scripts/upload-files Alcoholism $Alcoholism_BUCKET ./
nice -n 10 scripts/upload-files Alzheimers $Alzheimers_BUCKET ./
nice -n 10 scripts/upload-files Breast_Cancer $Breast_Cancer_BUCKET ./
nice -n 10 scripts/upload-files Colon_Cancer $Breast_Cancer_BUCKET ./
nice -n 10 scripts/upload-files Diabetes $Diabetes_BUCKET ./
nice -n 10 scripts/upload-files Lung_Cancer $Lung_Cancer_BUCKET ./
nice -n 10 scripts/upload-files Prostate_Cancer $Prostate_Cancer_BUCKET ./
nice -n 10 scripts/upload-files NVIDIA $NVIDIA_BUCKET ./output/home/exacloud/gscratch
# done


nice -n 10 scripts/upload-files NVIDIA aced-ohsu


# upload meta data to gen3
for study in ${synthetic_studies[*]}; do
  nice -10 scripts/load.py load graph --db_host localhost \
  --input_path /Users/walsbr/aced/submission/tmp/ \
  --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json \
  --project_code $study --dictionary_url https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json
done


for study in ${real_studies[*]}; do
  nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code $study
done

nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code HOP

nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code MCF10A

nice -10 scripts/load.py load graph --db_host localhost --sheepdog_creds_path ../compose-services-training/Secrets/sheepdog_creds.json --project_code NVIDIA

# load metadata to elastic

# Delete all from ES
# docker exec esproxy-service curl -X DELETE http://localhost:9200/gen3.aced.*

for study in ${synthetic_studies[*]}; do
  rm denormalized_patient.sqlite
  nice -10 python3 scripts/load.py  denormalize-patient --input_path studies/$study/extractions/
  nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson
  nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson
  nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index observation --path studies/$study/extractions/Observation.ndjson
done


study=HOP
rm denormalized_patient.sqlite
nice -10 python3 scripts/load.py  denormalize-patient --input_path studies/$study/extractions/
nice -10 python3 scripts/load.py load  flat --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson
nice -10 python3 scripts/load.py load  flat --project_id aced-$study --index observation --path studies/$study/extractions/Observation.ndjson


study=MCF10A
rm denormalized_patient.sqlite
nice -10 python3 scripts/load.py  denormalize-patient --input_path studies/$study/extractions/
nice -10 python3 scripts/load.py load  flat --project_id aced-$study --index patient --path studies/$study/extractions/Patient.ndjson
nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson
nice -10 python3 scripts/load.py load  flat --project_id aced-$study --index observation --path studies/$study/extractions/Observation.ndjson



study=NVIDIA
rm denormalized_patient.sqlite
nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson

study=ohsu_download_testing
rm denormalized_patient.sqlite
nice -10 python3 scripts/load.py  load  flat --project_id aced-$study --index file --path studies/$study/extractions/DocumentReference.ndjson



rm patient.sqlite
nice -10 python3 scripts/load.py load  flat --project_id HOP-CORE --index patient --path studies/CORE/extractions/Patient.ndjson
nice -10 python3 scripts/load.py load  flat --project_id HOP-CORE --index observation --path studies/CORE/extractions/Observation.ndjson


# python3 scripts/schema.py publish  --production


# for etl/POD

# create program and projects based on resources found in user.yaml
python3 scripts/load.py init

#
synthetic_studies=(Alcoholism Alzheimers Breast_Cancer Colon_Cancer Diabetes Lung_Cancer Prostate_Cancer ohsu_download_testing)

# setup environmental variables to connect directly to PG as sheepdog
export PGDB=`cat /creds/sheepdog-creds/database`
export PGPASSWORD=`cat /creds/sheepdog-creds/password`
export PGUSER=`cat /creds/sheepdog-creds/username`
export PGHOST=`cat /creds/sheepdog-creds/host`
export DBREADY=`cat /creds/sheepdog-creds/dbcreated`
export PGPORT=`cat /creds/sheepdog-creds/port`
echo e.g. Connecting $PGUSER:$PGPASSWORD@$PGHOST:$PGPORT//$PGDB if $DBREADY

# upload meta data to gen3
for study in ${synthetic_studies[*]}; do
  nice -10 scripts/load.py load graph   --project_code $study --dictionary_url https://aced-public.s3.us-west-2.amazonaws.com/aced-test.json
done
nice -10 scripts/load.py load graph  --project_code HOP
nice -10 scripts/load.py load graph  --project_code NVIDIA

export ES="--elastic_url http://$ELASTICSEARCH_SERVICE_HOST:$ELASTICSEARCH_SERVICE_PORT"
# curl -X DELETE http://$ELASTICSEARCH_SERVICE_HOST:$ELASTICSEARCH_SERVICE_PORT/gen3.aced.*
for study in ${synthetic_studies[*]}; do
  rm denormalized_patient.sqlite
  nice -10 python3 scripts/load.py  denormalize-patient --input_path studies/$study/extractions/
  nice -10 python3 scripts/load.py  load  flat $ES --project_id aced-$study --index patient --path /studies/$study/extractions/Patient.ndjson
  nice -10 python3 scripts/load.py  load  flat $ES --project_id aced-$study --index file --path /studies/$study/extractions/DocumentReference.ndjson
  nice -10 python3 scripts/load.py  load  flat $ES --project_id aced-$study --index observation --path /studies/$study/extractions/Observation.ndjson
done

study=HOP
nice -10 python3 scripts/load.py  load  flat $ES --project_id aced-$study --index patient --path /studies/$study/extractions/Patient.ndjson
nice -10 python3 scripts/load.py  load  flat $ES --project_id aced-$study --index observation --path /studies/$study/extractions/Observation.ndjson

study=NVIDIA
nice -10 python3 scripts/load.py  load  flat $ES --project_id aced-$study --index file --path /studies/$study/extractions/DocumentReference.ndjson
